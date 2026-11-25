"""Focused regression tests for verified Studio bug fixes.

Covers BUG-003, 020, 045, 046, 047, 048, 049, 056 and 059. Uses a temporary
SQLite database and Starlette TestClient; no network calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kmcp.db.manager import DBManager
from kmcp.studio import approvals, service_connections
from kmcp.studio.api import mount_studio_routes


class _ConfigProxy:
    """Attribute proxy so tests can override frozen config fields."""

    def __init__(self, base, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


@pytest.fixture
def db(tmp_path: Path):

    manager = DBManager(tmp_path / "bugfix_studio.sqlite")
    manager.ensure_schema()
    # Keep connection test / export tests independent of the host environment.
    yield manager
    manager.conn.close()


@pytest.fixture
def client(monkeypatch, db):
    from kmcp.config import config as real_config

    monkeypatch.setattr(
        "kmcp.studio.api.config", _ConfigProxy(real_config, api_token="", insecure=True)
    )
    monkeypatch.setattr("kmcp.studio.api.db_manager.get_db", lambda: db)
    app = Starlette()
    mount_studio_routes(app)
    return TestClient(app)


def _seed_published(db, playbook_id="pb1", status="published"):
    db.create_playbook(playbook_id, "Test playbook", status=status)
    vid = db.publish_playbook_version(
        playbook_id,
        "1.0.0",
        definition={"name": "Test playbook", "steps": []},
        published_by="test",
    )
    return vid


# ── BUG-003: publish approval gate ──────────────────────────────────────────


def test_publish_requires_terminal_approval_and_consumes_it(client, db, monkeypatch):
    _seed_published(db)
    valid = {"valid": True, "findings": []}
    monkeypatch.setattr("kmcp.studio.playbook_service.validate_definition", lambda *a, **k: valid)
    published = []
    monkeypatch.setattr(
        "kmcp.studio.playbook_service.publish_version",
        lambda *a, **k: published.append(1) or 42,
    )

    requested = client.post("/api/playbooks/pb1/approval-request")
    assert requested.status_code == 200
    approval_id = requested.json()["approval_id"]

    # A pending row is a request, not a decision.
    blocked = client.post(
        "/api/playbooks/pb1/publish", json={"definition": {"name": "x"}, "version": "2.0.0"}
    )
    assert blocked.status_code == 403
    assert not published

    decided = client.post(
        f"/api/approvals/{approval_id}/approve", headers={"Authorization": "Bearer operator"}
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    allowed = client.post(
        "/api/playbooks/pb1/publish", json={"definition": {"name": "x"}, "version": "2.0.0"}
    )
    assert allowed.status_code == 200
    assert allowed.json() == {"playbook_id": "pb1", "version_id": 42, "status": "published"}
    assert len(published) == 1

    # One approval authorizes exactly one publish.
    second = client.post(
        "/api/playbooks/pb1/publish", json={"definition": {"name": "x"}, "version": "2.0.0"}
    )
    assert second.status_code == 403
    assert len(published) == 1
    assert approvals.get_approved_approval(db, approvals.GATE_PUBLISH, "pb1") is None


def test_approval_decision_is_authenticated_and_non_repeatable(client, db, monkeypatch):
    from kmcp.config import config as real_config

    monkeypatch.setattr(
        "kmcp.studio.api.config", _ConfigProxy(real_config, api_token="secret", insecure=False)
    )
    aid = approvals.request_approval(db, gate="publish", resource_id="pb1", requested_by="alice")

    unauth = client.post(f"/api/approvals/{aid}/approve")
    assert unauth.status_code == 401

    headers = {"Authorization": "Bearer secret"}
    assert (
        client.post(f"/api/approvals/{aid}/reject", headers=headers).json()["status"] == "rejected"
    )
    # A rejected row cannot be flipped to approved afterwards.
    assert client.post(f"/api/approvals/{aid}/approve", headers=headers).status_code == 409
    assert approvals.is_approved(db, approvals.GATE_PUBLISH, "pb1") is False


# ── BUG-045: validation must not save/unpublish ─────────────────────────────


def test_validate_definition_is_read_only_but_save_still_works(client, db, monkeypatch):
    _seed_published(db, status="published")
    valid = {"valid": True, "findings": []}
    calls = []

    def fake_validate(*args, **kwargs):
        calls.append(kwargs.get("for_publish", False))
        return valid

    monkeypatch.setattr("kmcp.studio.playbook_service.validate_definition", fake_validate)

    r = client.post("/api/playbooks/pb1/validate-definition", json={"definition": {"name": "x"}})
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert calls == [False]
    row = db.get_playbook("pb1")
    assert row["status"] == "published"
    assert not row.get("draft_definition")

    # The dedicated save endpoint still writes a draft.
    saved = client.put("/api/playbooks/pb1/definition", json={"definition": {"name": "draft"}})
    assert saved.status_code == 200
    row = db.get_playbook("pb1")
    assert row["status"] == "draft"
    assert json.loads(row["draft_definition"]) == {"name": "draft"}


# ── BUG-020: connection test inside a running event loop ────────────────────


def test_connection_test_returns_structured_result(client, db, monkeypatch):
    service_connections.upsert_connection(
        db, "c1", "Local MCP", "sse", endpoint_url="http://127.0.0.1:9/mcp"
    )
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = None
    fake_client.list_tools.return_value = [{"name": "one"}]
    monkeypatch.setattr("kmcp.studio.mcp_client.ExternalMCPClient", lambda *a, **k: fake_client)

    ok = client.post("/api/connections/c1/test")
    assert ok.status_code == 200
    assert ok.json() == {
        "ok": True,
        "healthy": True,
        "tool_count": 1,
        "error": None,
    }
    health = db.conn.execute(
        "SELECT health_status FROM mcp_connections WHERE connection_id='c1'"
    ).fetchone()[0]
    assert health == "healthy"

    fake_client.list_tools = AsyncMock(side_effect=RuntimeError("token sk_live_secretvalue123"))
    bad = client.post("/api/connections/c1/test")
    assert bad.status_code == 200
    body = bad.json()
    assert body["ok"] is False and body["healthy"] is False
    assert "RuntimeError" in body["error"]
    assert "sk_live_secretvalue123" not in body["error"]
    health = db.conn.execute(
        "SELECT health_status FROM mcp_connections WHERE connection_id='c1'"
    ).fetchone()[0]
    assert health == "unreachable"


# ── BUG-047: resume must dispatch to the engine ─────────────────────────────


def test_resume_dispatches_to_checkpoint_engine(client, db, monkeypatch):
    from kmcp.workflow import dag_executor

    vid = _seed_published(db)
    db.create_workflow_execution("exec-1", "pb1", vid, inputs={"target": "host"}, status="failed")
    db.save_workflow_run(
        "exec-1", "pb1", "failed", [0], {"status": "failed", "completed_steps": [0]}
    )

    defn = SimpleNamespace(name="pb1", steps=[], inputs={}, max_parallel=2, timeout=300)
    monkeypatch.setattr("kmcp.workflow.yaml_loader.load_playbook", lambda definition: defn)
    monkeypatch.setattr(dag_executor, "RUN_STATUS", {})
    mock_run = AsyncMock(
        return_value=SimpleNamespace(
            stdout=json.dumps({"status": "completed", "step_results": {}}), success=True
        )
    )
    monkeypatch.setattr(dag_executor, "run_playbook_async", mock_run)

    r = client.post("/api/executions/exec-1/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["resumed"] is True and body["checkpoint_used"] is True
    assert dag_executor.RUN_STATUS["pb1"]["completed_steps"] == [0]
    mock_run.assert_awaited_once()
    assert mock_run.await_args.kwargs["resume"] is True
    assert db.get_workflow_execution("exec-1")["status"] == "completed"


def test_resume_without_checkpoint_does_not_report_success(client, db, monkeypatch):
    from kmcp.workflow import dag_executor

    vid = _seed_published(db)
    db.create_workflow_execution("exec-2", "pb1", vid, status="failed")
    defn = SimpleNamespace(name="pb1", steps=[], inputs={}, max_parallel=2, timeout=300)
    monkeypatch.setattr("kmcp.workflow.yaml_loader.load_playbook", lambda definition: defn)
    monkeypatch.setattr(dag_executor, "RUN_STATUS", {})
    mock_run = AsyncMock()
    monkeypatch.setattr(dag_executor, "run_playbook_async", mock_run)

    r = client.post("/api/executions/exec-2/resume")
    assert r.status_code == 400
    assert "checkpoint" in r.json()["error"]
    mock_run.assert_not_awaited()
    assert db.get_workflow_execution("exec-2")["status"] == "failed"


# ── BUG-046: findings pagination applied exactly once ───────────────────────


@pytest.fixture
def findings_db(db):
    cur = db.conn.cursor()
    cur.execute(
        "INSERT INTO targets (target, target_type, scope, created_at, updated_at) "
        "VALUES ('host', 'host', 'in-scope', 'now', 'now')"
    )
    target_id = cur.lastrowid
    for title in ["alpha", "bravo", "charlie", "delta", "echo"]:
        cur.execute(
            "INSERT INTO findings (target_id, tool, finding_type, severity, title, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (target_id, "nmap", "port", "info", title, "2026-01-01T00:00:00", "now"),
        )
    db.conn.commit()
    return db


def test_findings_pagination_and_server_side_sort(client, findings_db):
    page1 = client.get("/api/findings?limit=2&offset=0").json()
    page2 = client.get("/api/findings?limit=2&offset=2").json()
    assert page1["count"] == 5
    assert [f["title"] for f in page1["findings"]] == ["alpha", "bravo"]
    assert [f["title"] for f in page2["findings"]] == ["charlie", "delta"]

    sorted_page = client.get("/api/findings?sort=title&dir=desc&limit=2&offset=0").json()
    assert [f["title"] for f in sorted_page["findings"]] == ["echo", "delta"]
    sorted_page2 = client.get("/api/findings?sort=title&dir=desc&limit=2&offset=2").json()
    assert [f["title"] for f in sorted_page2["findings"]] == ["charlie", "bravo"]


def test_frontend_renders_backend_page_without_reslicing():
    source = Path("src/kmcp/studio/web/app.js").read_text(encoding="utf-8")
    assert "const pageRows = findingsCache; // backend already applied sort + pagination" in source
    assert "sorted.slice(" not in source
    assert 'params.set("sort", sortField)' in source


# ── BUG-048/049: audit + redaction ──────────────────────────────────────────


def test_audit_records_mutating_handler_with_actor(client, db, monkeypatch):
    _seed_published(db)
    monkeypatch.setattr(
        "kmcp.studio.playbook_service.validate_definition",
        lambda *a, **k: {"valid": True, "findings": []},
    )
    r = client.put(
        "/api/playbooks/pb1/definition",
        json={"definition": {"name": "draft"}},
        headers={"Authorization": "Bearer operator-token"},
    )
    assert r.status_code == 200
    audit = client.get("/api/audit").json()["audit"]
    events = [row["event"] for row in audit]
    assert "playbook.draft_saved" in events
    row = next(row for row in audit if row["event"] == "playbook.draft_saved")
    assert row["actor"].startswith("bearer:")
    assert row["entity_id"] == "pb1"
    assert "operator-token" not in json.dumps(audit)


def test_auth_config_values_are_never_returned(client, db):
    secret = "sk_live_supersecretvalue123"
    service_connections.upsert_connection(
        db, "c2", "Remote", "sse", auth_config={"api_key": secret, "header_name": "X-Key"}
    )
    for response in (
        client.get("/api/connections/c2"),
        client.get("/api/connections"),
        client.put("/api/connections/c2", json={"name": "Remote renamed"}),
    ):
        assert response.status_code == 200
        assert secret not in response.text
        body = response.json()
        conn = body.get("connection") or (body.get("connections") or [body])[0]
        assert conn["auth_config"] == {
            "configured": True,
            "fields": ["api_key", "header_name"],
        }


def test_step_outputs_are_redacted(db):
    from kmcp.studio.service_executions import get_execution

    vid = _seed_published(db)
    db.create_workflow_execution("exec-3", "pb1", vid, status="completed")
    db.conn.cursor().execute(
        "UPDATE workflow_executions SET outputs=? WHERE execution_id=?",
        (
            json.dumps({"password": "hunter2", "nested": {"token": "sk_live_abcdefghijkl"}}),
            "exec-3",
        ),
    )
    db.conn.cursor().execute(
        "INSERT INTO workflow_step_executions (execution_id, step_id, status, outputs) "
        "VALUES (?,?,?,?)",
        ("exec-3", "s1", "completed", json.dumps({"api_key": "supersecretkeyvalue"})),
    )
    db.conn.commit()
    data = get_execution(db, "exec-3")
    assert data["outputs"]["password"] == "***REDACTED***"
    assert data["outputs"]["nested"]["token"] == "***REDACTED***"
    assert data["steps"][0]["outputs"]["api_key"] == "***REDACTED***"


# ── BUG-056: server auth flag ───────────────────────────────────────────────


def test_server_info_reports_real_auth_config(client, monkeypatch):
    from kmcp.config import config as real_config

    monkeypatch.setattr(
        "kmcp.studio.api.config", _ConfigProxy(real_config, api_token="secret", insecure=False)
    )
    assert client.get("/api/server").json()["auth_enabled"] is True
    monkeypatch.setattr(
        "kmcp.studio.api.config", _ConfigProxy(real_config, api_token="secret", insecure=True)
    )
    assert client.get("/api/server").json()["auth_enabled"] is False
    monkeypatch.setattr(
        "kmcp.studio.api.config", _ConfigProxy(real_config, api_token="", insecure=False)
    )
    assert client.get("/api/server").json()["auth_enabled"] is False


# ── BUG-059: invalid integers return 400 ────────────────────────────────────


def test_invalid_integer_parameters_return_400(client, db):
    assert client.get("/api/playbooks?limit=abc").status_code == 400
    assert client.get("/api/playbooks?offset=-1").status_code == 400
    assert client.get("/api/executions?limit=oops").status_code == 400
    assert client.get("/api/tools?limit=-5").status_code == 400
    assert client.get("/api/findings?offset=nope").status_code == 400
    assert client.put("/api/findings/not-an-int", json={"status": "resolved"}).status_code == 400
    assert client.delete("/api/findings/-3").status_code == 400
