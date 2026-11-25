"""Regressions for verified DB-manager bugs: BUG-028/029/030/032.

All tests use temporary databases; the project data directory is never touched.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from kmcp.db.manager import DBManager


@pytest.fixture()
def db(tmp_path):
    manager = DBManager(tmp_path / "bugfix.sqlite")
    manager.ensure_schema()
    yield manager
    manager.close()


class TestDeletePlaybook:
    """BUG-028: versions are FK-referenced by executions/nodes/edges."""

    def test_delete_after_execution_clears_dependents(self, db):
        db.create_playbook("pb_exec", "Executed playbook")
        version_id = db.publish_playbook_version("pb_exec", "1", {"steps": []})
        db.create_workflow_execution("exec-1", "pb_exec", version_id, job_id="job-1")
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO workflow_nodes (version_id, node_id, node_type) VALUES (?,?,?)",
            (version_id, "n1", "tool"),
        )
        cur.execute(
            "INSERT INTO workflow_edges "
            "(version_id, source_node_id, target_node_id, edge_type) VALUES (?,?,?,?)",
            (version_id, "n1", "n2", "flow"),
        )
        cur.execute(
            "INSERT INTO workflow_step_executions (execution_id, step_id) VALUES (?,?)",
            ("exec-1", "s1"),
        )
        db.conn.commit()

        # The old implementation raised sqlite3.IntegrityError here.
        assert db.delete_playbook("pb_exec") is True

        assert db.get_playbook("pb_exec") is None
        for table, column, value in (
            ("playbook_versions", "id", version_id),
            ("workflow_executions", "version_id", version_id),
            ("workflow_nodes", "version_id", version_id),
            ("workflow_edges", "version_id", version_id),
            ("workflow_step_executions", "execution_id", "exec-1"),
        ):
            remaining = db.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)
            ).fetchone()[0]
            assert remaining == 0, f"{table} still has rows for {value}"
        assert db.delete_playbook("pb_exec") is False

    def test_delete_unexecuted_playbook_still_works(self, db):
        db.create_playbook("pb_plain", "Draft")
        db.publish_playbook_version("pb_plain", "1", {"steps": []})
        assert db.delete_playbook("pb_plain") is True
        assert db.list_playbook_versions("pb_plain") == []


class TestDeleteToolDefinition:
    """BUG-029: stale grants silently re-authorize a recreated tool id."""

    def test_deleting_definition_removes_its_grants(self, db):
        db.upsert_tool_definition("tool_x", "Tool X")
        db.create_agent_profile("profile_a", "Profile A")
        db.grant_tool_permission("profile_a", "tool_x")
        db.grant_tool_permission("profile_a", "unrelated_tool")
        assert db.can_agent_call_tool("profile_a", "tool_x") is True

        assert db.delete_tool_definition("tool_x") is True

        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM agent_tool_permissions WHERE tool_id = ?", ("tool_x",)
            ).fetchone()[0]
            == 0
        )
        # Unrelated grants must survive.
        assert db.can_agent_call_tool("profile_a", "unrelated_tool") is True
        # Recreating the id must not inherit the old authorization.
        db.upsert_tool_definition("tool_x", "Tool X v2")
        assert db.can_agent_call_tool("profile_a", "tool_x") is False
        assert db.delete_tool_definition("tool_x") is True
        assert db.delete_tool_definition("tool_x") is False


class TestFindingCorrelation:
    """BUG-032: lastrowid is stale after INSERT OR IGNORE skips a duplicate."""

    def test_duplicate_returns_existing_correlation_row_id(self, db):
        tid = db.upsert_target("host")
        sid = db.add_scan(tid, "nmap", "nmap", "s", "e", 0.0, 1, 0, None)
        fid = db.add_finding(tid, sid, "nmap", "port", "low", "title", "", "")
        first = db.add_finding_correlation("corr_dup", fid, fid, "nmap")

        # Advance the connection-wide lastrowid through an unrelated insert so
        # an ignored INSERT OR IGNORE would otherwise return that foreign id.
        db.upsert_target("another-host")
        second = db.add_finding_correlation("corr_dup", fid, fid, "nmap")

        existing = db.conn.execute(
            "SELECT id FROM finding_correlation WHERE correlation_id = ?", ("corr_dup",)
        ).fetchone()
        assert existing is not None
        assert first == second == int(existing[0])


class TestThreadSafety:
    """BUG-030: one connection shared across threads needs a lock."""

    def test_commit_does_not_publish_another_threads_uncommitted_writes(self, tmp_path):
        db = DBManager(tmp_path / "concurrent.sqlite")
        db.ensure_schema()

        real_conn = db.conn
        at_pause = threading.Event()
        boundary = threading.Barrier(2)  # paused writer <-> test thread
        b_finished = threading.Event()
        errors: list[tuple[str, BaseException]] = []
        thread_a: list[threading.Thread | None] = [None]

        class PausingConnection:
            """Pause writer A after its INSERT, immediately before commit()."""

            def __init__(self) -> None:
                self._guard = threading.Lock()
                self._paused = False

            def __getattr__(self, name):
                return getattr(real_conn, name)

            def commit(self):
                with self._guard:
                    first = not self._paused
                    self._paused = True
                if first and threading.current_thread() is thread_a[0]:
                    at_pause.set()
                    boundary.wait(timeout=10)
                return real_conn.commit()

        db.conn = PausingConnection()

        def writer_a() -> None:
            try:
                db.upsert_target("uncommitted-by-a")
            except BaseException as exc:  # noqa: BLE001
                errors.append(("a", exc))

        thread_a[0] = threading.Thread(target=writer_a, name="writer-a")
        thread_a[0].start()
        assert at_pause.wait(5), "writer A never reached its pre-commit pause"

        def writer_b() -> None:
            try:
                db.upsert_target("committed-by-b")
            except BaseException as exc:  # noqa: BLE001
                errors.append(("b", exc))
            finally:
                b_finished.set()

        thread_b = threading.Thread(target=writer_b, name="writer-b")
        thread_b.start()

        # Give B a chance to commit while A is still paused. With the buggy
        # shared connection that commit also publishes A's pending row; with
        # either valid fix it cannot.
        b_finished.wait(0.5)

        observer = sqlite3.connect(str(db.path))
        try:
            visible_from_other_connection = observer.execute(
                "SELECT COUNT(*) FROM targets WHERE target = ?", ("uncommitted-by-a",)
            ).fetchone()[0]
        finally:
            observer.close()

        boundary.wait(timeout=5)  # let A finish its commit
        thread_a[0].join(timeout=10)
        thread_b.join(timeout=10)
        db.close()

        assert not errors, errors
        assert not thread_a[0].is_alive() and not thread_b.is_alive()
        assert visible_from_other_connection == 0, "A's uncommitted write was made durable by B"

        # Both writes are still persisted once each operation has finished.
        check = DBManager(tmp_path / "concurrent.sqlite")
        try:
            targets = {row[0] for row in check.conn.execute("SELECT target FROM targets")}
        finally:
            check.close()
        assert {"uncommitted-by-a", "committed-by-b"} <= targets

    def test_concurrent_crud_persists_every_write(self, tmp_path):
        db = DBManager(tmp_path / "stress.sqlite")
        db.ensure_schema()
        barrier = threading.Barrier(2)
        errors: list[tuple[str, BaseException]] = []
        ids: dict[str, list[int]] = {"a": [], "b": []}

        def worker(tag: str) -> None:
            try:
                barrier.wait()  # maximize overlap between the two writers
                for i in range(25):
                    tid = db.upsert_target(f"{tag}-{i}")
                    ids[tag].append(tid)
                    db.add_scan(tid, "tool", "cmd", "s", "e", 0.0, 1, 0, None)
            except BaseException as exc:  # noqa: BLE001
                errors.append((tag, exc))

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        db.close()

        assert not errors, errors
        assert len(set(ids["a"] + ids["b"])) == 50
        check = DBManager(tmp_path / "stress.sqlite")
        try:
            assert check.conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 50
            assert check.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 50
        finally:
            check.close()
