"""Unit tests for the new Studio API routes added in the web-app overhaul:

* ``PUT /api/server/debug`` (runtime debug toggle)
* ``POST /api/tools/{id}/run`` (single-tool test run)

Plus assertions on ``GET /api/server`` (debug + health passthrough) and
``GET /api/tools`` (input_schema parsing + alias_for passthrough).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kmcp.studio.api import mount_studio_routes


class FakeDB:
    """Minimal stand-in for DBManager used by the routes under test."""

    def __init__(self, tools=None) -> None:
        self.tools = {t["tool_id"]: dict(t) for t in (tools or [])}

    def get_db(self):  # mirror db_manager.get_db() shape
        return self

    def list_tool_definitions(self, **kwargs):
        return list(self.tools.values())

    def get_tool_definition(self, tool_id):
        return self.tools.get(tool_id)

    def upsert_tool_definition(self, **kwargs):
        self.tools[kwargs["tool_id"]] = kwargs
        return 1


class FakeTool:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.name = "fake-tool"

    async def safe_execute(self, arguments):
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.result or {}, echo=arguments)


def _make_app(monkeypatch, tools=None, health=None):
    app = Starlette()
    mount_studio_routes(app)
    fake_db = FakeDB(tools)
    monkeypatch.setattr("kmcp.studio.api.db_manager.get_db", lambda: fake_db)
    if health is not None:
        monkeypatch.setattr("kmcp.studio.api._server_health", lambda: health)
    return app, fake_db


@pytest.fixture
def client(monkeypatch):
    app, _ = _make_app(
        monkeypatch,
        tools=[
            {
                "tool_id": "nuclei",
                "name": "nuclei",
                "description": "vuln scanner",
                "provider": "native",
                "visibility": "agent_callable",
                "enabled": 1,
                "input_schema": '{"type":"object","properties":{"target":{"type":"string"}},"required":["target"]}',
                "default_timeout": 300,
            },
            {
                "tool_id": "search",
                "name": "search",
                "description": "alias",
                "provider": "native",
                "visibility": "internal",
                "enabled": 1,
                "input_schema": {},
                "default_timeout": 30,
                # no alias_for column in the DB yet → key must still be present as None
            },
        ],
    )
    return TestClient(app)


def test_server_info_includes_debug(client):
    r = client.get("/api/server")
    assert r.status_code == 200
    body = r.json()
    assert "debug" in body


def test_put_server_debug_toggle_roundtrip(client):
    r = client.put("/api/server/debug", json={"debug": True})
    assert r.status_code == 200
    assert r.json() == {"debug": True, "ok": True}

    # Reflected by GET /api/server (runtime store, config is a frozen dataclass)
    r = client.get("/api/server")
    assert r.json()["debug"] is True

    r = client.put("/api/server/debug", json={"debug": False})
    assert r.status_code == 200
    assert r.json()["debug"] is False
    assert client.get("/api/server").json()["debug"] is False


def test_put_server_debug_invalid_json(client):
    r = client.put("/api/server/debug", content=b"not-json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_list_tools_parses_input_schema_and_alias_for(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    ids = {t["tool_id"] for t in body["tools"]}
    assert ids == {"nuclei", "search"}
    nuclei = next(t for t in body["tools"] if t["tool_id"] == "nuclei")
    assert nuclei["input_schema"] == {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    }
    assert nuclei["alias_for"] is None  # column doesn't exist yet; key is present


def test_put_tool_visibility_success_and_not_found(client):
    r = client.put("/api/tools/nuclei/visibility", json={"visibility": "internal"})
    assert r.status_code == 200
    assert r.json() == {"tool_id": "nuclei", "visibility": "internal"}

    r = client.put("/api/tools/does-not-exist/visibility", json={"visibility": "internal"})
    assert r.status_code == 404

    r = client.put("/api/tools/nuclei/visibility", json={"visibility": "bogus"})
    assert r.status_code == 400

    r = client.put("/api/tools/nuclei/visibility", json={})
    assert r.status_code == 400


def test_run_tool_executes_via_safe_execute(client, monkeypatch):
    fake_tool = FakeTool(result={"ok": True, "matches": 3})
    monkeypatch.setattr("kmcp.registry.registry.get", lambda name: fake_tool)

    r = client.post("/api/tools/nuclei/run", json={"args": {"target": "example.com"}})
    assert r.status_code == 200
    body = r.json()
    assert body["tool_id"] == "nuclei"
    assert body["result"] == {"ok": True, "matches": 3, "echo": {"target": "example.com"}}


def test_run_tool_passes_empty_args_and_404s(client, monkeypatch):
    calls = []
    known = {"nuclei": FakeTool(result={"ran": True})}

    def fake_get(name):
        calls.append(name)
        return known.get(name)

    monkeypatch.setattr("kmcp.registry.registry.get", fake_get)

    r = client.post("/api/tools/nuclei/run", json={})
    assert r.status_code == 200
    assert r.json()["result"] == {"ran": True, "echo": {}}
    assert calls == ["nuclei"]

    r = client.post("/api/tools/unknown-tool/run", json={"args": {}})
    assert r.status_code == 404

    r = client.post("/api/tools/nuclei/run", json={"args": [1, 2]})  # args must be an object
    assert r.status_code == 400

