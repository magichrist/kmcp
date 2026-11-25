"""Focused regressions for builder-handoff defects F-01, F-02, F-09, F-11, F-12.

All tests are hermetic: temporary SQLite databases, mocked DB/vault/engine
access and in-memory registries. No network calls and no full-suite side effects.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kmcp.models import ExecutionResult, utc_now_iso
from kmcp.tools import agent_interface as ai
from kmcp.tools.agent_interface import (
    AGENT_MODE_TOOLS,
    AssessmentRunTool,
    BatchRunTool,
    FindingTransitionTool,
    JobControlTool,
    ToolCatalogTool,
)


def _payload(res: dict) -> dict:
    outer = json.loads(res["content"][0]["text"])
    return json.loads(outer["stdout"])


def _exec_result(stdout: dict | None = None) -> ExecutionResult:
    return ExecutionResult(
        tool="test",
        command="test",
        stdout=json.dumps(stdout or {"step_results": {}}),
        stderr="",
        exit_code=0,
        success=True,
        timed_out=False,
        duration=0.0,
        start_time=utc_now_iso(),
        end_time=utc_now_iso(),
    )


class RecordingConn:
    """Real sqlite3 connection that records SQL and commits."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0

    def execute(self, sql: str, params=()):
        self.executed.append((sql, tuple(params or ())))
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        self.commits += 1
        self._conn.commit()


@pytest.fixture()
def findings_db(tmp_path, monkeypatch):
    """Temp DB with one finding in the observed state; returns (conn, db)."""
    path = tmp_path / "findings.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE findings (id INTEGER PRIMARY KEY, validation_status TEXT, updated_at TEXT)"
    )
    raw.execute(
        "INSERT INTO findings (id, validation_status, updated_at) VALUES (?, ?, ?)",
        (1, "observed", "2020-01-01T00:00:00Z"),
    )
    raw.commit()
    conn = RecordingConn(raw)
    db = SimpleNamespace(conn=conn)
    monkeypatch.setattr(ai.dbm, "get_db", lambda: db)
    yield conn, db
    raw.close()


def _row(conn: RecordingConn) -> tuple:
    return conn._conn.execute(
        "SELECT validation_status, updated_at FROM findings WHERE id = 1"
    ).fetchone()


# ── F-01: finding_transition must not write rejected transitions ───────────


async def test_f01_invalid_transition_does_not_mutate_db(findings_db):
    conn, _ = findings_db
    before = _row(conn)

    res = await FindingTransitionTool().execute(
        {"finding_id": 1, "new_state": "remediated", "source": "analyst"}
    )
    payload = _payload(res)

    assert payload["success"] is False
    assert payload["previous_state"] == "observed"
    assert "Invalid transition" in payload["error"]
    assert _row(conn) == before
    assert not any(sql.lstrip().upper().startswith("UPDATE") for sql, _ in conn.executed)
    assert conn.commits == 0


async def test_f01_integer_finding_id_valid_transition_persists(findings_db):
    conn, _ = findings_db
    before = _row(conn)

    res = await FindingTransitionTool().execute(
        {"finding_id": 1, "new_state": "suspected", "source": "analyst", "reason": "triage"}
    )
    payload = _payload(res)

    assert payload["success"] is True
    assert payload["finding_id"] == "1"
    assert payload["previous_state"] == "observed"
    after = _row(conn)
    assert after[0] == "suspected"
    assert after[1] != before[1]
    assert conn.commits == 1
    assert payload["transition_history"][0]["finding_id"] == "1"


def test_f01_schema_accepts_string_and_integer_ids():
    schema = FindingTransitionTool().input_schema()["properties"]["finding_id"]["type"]
    assert "string" in schema and "integer" in schema


# ── F-02: playbook loading and structured failure payloads ─────────────────


def test_f02_all_workflows_resolve_to_packaged_yaml():
    tool = AssessmentRunTool()
    packaged = Path(ai.__file__).resolve().parents[1] / "playbooks" / "definitions"
    for workflow, filename in tool._WORKFLOW_PLAYBOOKS.items():
        assert (packaged / filename).is_file(), f"{workflow} -> {filename} missing"
        assert tool._load_workflow_playbook(workflow) is not None


def test_f02_loader_prefers_packaged_playbook_dir(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("kmcp.workflow.load_playbook", lambda path: seen.append(path) or "pb")
    pb = AssessmentRunTool()._load_workflow_playbook("web_assessment")
    assert pb == "pb"
    packaged = Path(ai.__file__).resolve().parents[1] / "playbooks" / "definitions"
    assert Path(seen[0]).parent == packaged


def test_f02_missing_playbook_file_has_clear_error(monkeypatch):
    monkeypatch.setitem(
        AssessmentRunTool._WORKFLOW_PLAYBOOKS, "missing_workflow", "does_not_exist.yaml"
    )
    with pytest.raises(FileNotFoundError, match="does_not_exist.yaml"):
        AssessmentRunTool()._load_workflow_playbook("missing_workflow")


async def test_f02_inline_failure_returns_structured_payload(monkeypatch, caplog):
    monkeypatch.setattr(ai.dbm, "get_db", lambda: None)
    monkeypatch.setattr(ai, "get_assessment_context", lambda: (None, None))
    monkeypatch.setattr(
        ai, "run_playbook_async", AsyncMock(side_effect=RuntimeError("kaboom"))
    )
    writes = []
    monkeypatch.setattr(ai, "vault_write", lambda **kw: writes.append(kw))

    with caplog.at_level(logging.ERROR, logger="kmcp.tools"):
        res = await AssessmentRunTool().execute(
            {
                "workflow": "web_assessment",
                "targets": ["example.test"],
                "execution": {"max_duration": 60},
            }
        )
    payload = _payload(res)

    assert payload["status"] == "failed"
    assert payload["assessment_id"].startswith("assessment_")
    assert payload["error"] == {"type": "RuntimeError", "message": "kaboom"}
    assert "RuntimeError: kaboom" in caplog.text
    assert writes and writes[-1]["content"]["status"] == "failed"


async def test_f02_background_failure_writes_structured_error(monkeypatch, caplog):
    writes = []
    monkeypatch.setattr(ai, "vault_write", lambda **kw: writes.append(kw))
    monkeypatch.setattr(
        AssessmentRunTool,
        "_run_assessment_inline",
        AsyncMock(side_effect=RuntimeError("bg boom")),
    )

    with caplog.at_level(logging.ERROR, logger="kmcp.tools"):
        await AssessmentRunTool()._run_assessment_background(
            "a1", "j1", "web_assessment", ["x"], None, "safe_active", 4, 900, False, "summary", False
        )

    content = writes[-1]["content"]
    assert content["assessment_id"] == "a1"
    assert content["status"] == "failed"
    assert content["error"] == {"type": "RuntimeError", "message": "bg boom"}
    assert "RuntimeError: bg boom" in caplog.text


# ── F-09: assessments vault summary on both completion paths ──────────────


async def test_f09_inline_success_writes_assessment_summary(monkeypatch):
    monkeypatch.setattr(ai.dbm, "get_db", lambda: None)
    monkeypatch.setattr(ai, "get_assessment_context", lambda: ("a-inline", None))
    monkeypatch.setattr(ai, "run_playbook_async", AsyncMock(return_value=_exec_result()))
    writes = []
    monkeypatch.setattr(ai, "vault_write", lambda **kw: writes.append(kw))

    res = await AssessmentRunTool().execute(
        {
            "workflow": "web_assessment",
            "targets": ["example.test"],
            "execution": {"max_duration": 60},
        }
    )
    payload = _payload(res)

    assert payload["status"] == "completed"
    content = [w for w in writes if w["category"] == "assessments"][-1]["content"]
    assert content["assessment_id"] == payload["assessment_id"] == "a-inline"
    assert content["status"] == "completed"
    assert "summary" in content and "findings" in content and "next_actions" in content


async def test_f09_background_success_writes_assessment_summary(monkeypatch):
    writes = []
    monkeypatch.setattr(ai, "vault_write", lambda **kw: writes.append(kw))
    monkeypatch.setattr(
        AssessmentRunTool,
        "_run_assessment_inline",
        AsyncMock(return_value=([], [], ["act"])),
    )

    await AssessmentRunTool()._run_assessment_background(
        "a-bg", "j-bg", "web_assessment", ["x"], None, "safe_active", 4, 900, False, "summary", False
    )

    content = writes[-1]["content"]
    assert content["assessment_id"] == "a-bg"
    assert content["job_id"] == "j-bg"
    assert content["status"] == "completed"
    assert content["next_actions"] == ["act"]


# ── F-11: job_control resolves parallel/batch job ids ─────────────────────


@pytest.fixture()
def clean_job_registries():
    ai.BATCH_JOBS.clear()
    from kmcp.tools.parallel_execute import JOBS

    JOBS.clear()
    yield
    ai.BATCH_JOBS.clear()
    JOBS.clear()


async def test_f11_job_control_resolves_parallel_job(monkeypatch, clean_job_registries):
    from kmcp.tools.parallel_execute import JOBS

    JOBS["par_123"] = {"status": "completed", "total": 2, "done": 2}
    monkeypatch.setattr(ai, "vault_query", lambda **kw: {"entries": []})
    monkeypatch.setattr(ai.engine, "get_active_jobs", list)

    res = await JobControlTool().execute({"job_id": "par_123", "action": "status"})
    payload = _payload(res)

    assert payload["status"] == "completed"
    assert payload["tool"] == "parallel_execute"
    assert payload["result"]["done"] == 2


async def test_f11_job_control_resolves_batch_job(monkeypatch, clean_job_registries):
    fake_tool = SimpleNamespace(
        safe_execute=AsyncMock(
            return_value={"content": [{"type": "text", "text": "{}"}], "isError": False}
        )
    )
    monkeypatch.setattr("kmcp.registry.registry.get", lambda name: fake_tool)
    monkeypatch.setattr(ai, "vault_query", lambda **kw: {"entries": []})
    monkeypatch.setattr(ai.engine, "get_active_jobs", list)

    batch_res = await BatchRunTool().execute(
        {"operations": [{"tool": "nmap", "args": {"target": "x"}}]}
    )
    batch_id = _payload(batch_res)["batch_id"]

    res = await JobControlTool().execute({"job_id": batch_id, "action": "status"})
    payload = _payload(res)

    assert payload["status"] == "completed"
    assert payload["tool"] == "batch_run"
    assert payload["result"]["batch_id"] == batch_id

    cancel = await JobControlTool().execute({"job_id": batch_id, "action": "cancel"})
    assert _payload(cancel)["status"] == "cancelled"
    assert ai.BATCH_JOBS[batch_id]["status"] == "cancelled"


async def test_f11_unknown_parallel_id_still_not_found(monkeypatch, clean_job_registries):
    monkeypatch.setattr(ai, "vault_query", lambda **kw: {"entries": []})
    monkeypatch.setattr(ai.engine, "get_active_jobs", list)
    res = await JobControlTool().execute({"job_id": "par_missing"})
    assert _payload(res)["status"] == "not_found"


# ── F-12: expert catalog uses the full tool registry ───────────────────────


class FakeCatalogTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"tool {name}"

    def input_schema(self):
        return {"type": "object", "properties": {}}

    @property
    def default_timeout(self) -> int:
        return 30


async def test_f12_expert_lists_full_registry_agent_mode_stays_filtered(monkeypatch):
    from kmcp.registry import registry

    names = [f"extra_{i:03d}" for i in range(76)] + ["nmap", "generic_command"]
    tools = [FakeCatalogTool(name) for name in names]
    monkeypatch.setattr(registry, "list_all", lambda: tools)
    monkeypatch.setattr(registry, "get_definition", lambda name: None)
    monkeypatch.setattr(ai.agent_context, "get_current_profile", lambda: None)

    expert = _payload(await ToolCatalogTool().execute({"mode": "expert"}))
    assert expert["total"] == len(names)
    assert {t["name"] for t in expert["tools"]} == set(names)

    agent = _payload(await ToolCatalogTool().execute({"mode": "agent"}))
    agent_names = {t["name"] for t in agent["tools"]}
    assert agent_names == set(names) & AGENT_MODE_TOOLS
    assert "nmap" in agent_names
    assert "generic_command" not in agent_names
