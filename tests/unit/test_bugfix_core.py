"""Focused regression tests for verified core security / lifecycle bugs.

Each test targets one of BUG-001, BUG-004, BUG-005, BUG-006, BUG-023,
BUG-024, BUG-025, BUG-026, BUG-027 and BUG-057 from ``docs/bugs.md``.
All I/O is mocked or confined to pytest tmp directories.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from kmcp import agent_context
from kmcp import server as server_module
from kmcp.config import config
from kmcp.evidence import store_evidence
from kmcp.execution import MAX_OUTPUT, ExecutionEngine, _spawn_isolated_tracked
from kmcp.isolation import StepLimits
from kmcp.scope import UNKNOWN_TARGET, Scope, check_scope
from kmcp.tools.persist import extract_target


@pytest.fixture(autouse=True)
def _reset_profile_ctx():
    token = agent_context.set_current_profile(None)
    yield
    agent_context.reset_current_profile(token)


@pytest.fixture
def cfg_patch():
    """Temporarily override frozen config fields; restore every field after."""
    originals: dict[str, object] = {}

    def _set(**kwargs):
        for key, value in kwargs.items():
            originals.setdefault(key, getattr(config, key))
            object.__setattr__(config, key, value)

    yield _set
    for key, value in originals.items():
        object.__setattr__(config, key, value)


def _ctx(headers=None):
    """Minimal FastMCP Context stand-in with optional HTTP headers."""
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers=headers or {})),
        session=None,
        report_progress=AsyncMock(),
    )


class _FakeDB:
    def __init__(self, profiles=(), grants=()):
        self.profiles = set(profiles)
        self.grants = set(grants)

    def get_agent_profile(self, profile_id):
        return {"id": profile_id} if profile_id in self.profiles else None

    def can_agent_call_tool(self, profile_id, tool_id):
        return (profile_id, tool_id) in self.grants


# ── BUG-001 / BUG-027: auth on the dedicated Studio port + CORS ordering ──


def test_bug001_studio_app_carries_same_token_auth(monkeypatch, cfg_patch):
    monkeypatch.setattr("kmcp.studio.api.mount_studio_routes", lambda app: None)
    cfg_patch(insecure=False)
    studio_app = server_module._build_studio_app("studio-secret")
    mws = [m for m in studio_app.user_middleware if m.cls is server_module.TokenAuthMiddleware]
    assert len(mws) == 1
    assert mws[0].kwargs["token"] == "studio-secret"
    assert mws[0].kwargs["open_paths"] == ("/studio",)

    # No token (dev/insecure mode) → no gate, unchanged behavior.
    cfg_patch(insecure=True)
    open_app = server_module._build_studio_app("")
    assert not any(m.cls is server_module.TokenAuthMiddleware for m in open_app.user_middleware)


def test_bug027_cors_is_outermost_over_auth(monkeypatch, cfg_patch):
    cfg_patch(insecure=False, allowed_origins="http://example.com")
    app = Starlette()
    server_module._install_http_middleware(app, "secret")
    assert app.user_middleware[0].cls is CORSMiddleware
    assert any(m.cls is server_module.TokenAuthMiddleware for m in app.user_middleware)


def test_bug027_options_preflight_is_not_rejected(monkeypatch, cfg_patch):
    cfg_patch(insecure=False, allowed_origins="http://example.com")
    app = Starlette(
        routes=[Route("/api/x", lambda request: JSONResponse({"ok": True}), methods=["POST"])]
    )
    server_module._install_http_middleware(app, "secret")
    client = TestClient(app)
    resp = client.options(
        "/api/x",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://example.com"
    # A real POST still needs the token.
    assert client.post("/api/x").status_code == 401


# ── BUG-004: evidence path traversal ───────────────────────────────────────


def test_bug004_evidence_path_is_sanitized_and_contained(tmp_path, cfg_patch, monkeypatch):
    cfg_patch(artifact_dir=tmp_path / "artifacts")
    monkeypatch.setattr("kmcp.db.manager.get_db", lambda: None)
    monkeypatch.setattr("kmcp.utils.vault.vault_write", lambda *a, **k: None)

    evidence_id = store_evidence(
        tool="nmap",
        target="127.0.0.1",
        artifact_type="../../etc_cron",
        content="pwned",
        assessment_id="../../outside",
    )
    base = (tmp_path / "artifacts" / "evidence").resolve()
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "evidence file should still be written"
    assert all(p.resolve().is_relative_to(base) for p in written)
    assert not (tmp_path / "outside").exists()
    assert len(list(base.rglob(f"{evidence_id}_*.txt"))) == 1


# ── BUG-005: /download arbitrary file read ──────────────────────────────────


def test_bug005_download_restricted_to_artifact_dir(tmp_path, cfg_patch):
    cfg_patch(artifact_dir=tmp_path / "artifacts")
    artifact = tmp_path / "artifacts" / "hello.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("hello")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")

    app = Starlette(routes=[Route("/download", server_module.download_file, methods=["GET"])])
    client = TestClient(app)
    assert client.get("/download", params={"path": str(secret)}).status_code == 403
    assert client.get("/download", params={"path": "/etc/passwd"}).status_code == 403
    ok = client.get("/download", params={"path": str(artifact)})
    assert ok.status_code == 200
    assert ok.text == "hello"


# ── BUG-006: target extraction + CIDR scope ─────────────────────────────────


def test_bug006_extract_target_covers_url_ip_and_nested_params():
    assert extract_target({"url": "http://10.0.0.5"}) == "http://10.0.0.5"
    assert extract_target({"ip": "10.0.0.5"}) == "10.0.0.5"
    assert extract_target({"hostname": "host.local"}) == "host.local"
    assert extract_target({"params": {"url": "http://10.0.0.5"}}) == "http://10.0.0.5"
    assert extract_target({"args": {"ip": "10.0.0.5"}}) == "10.0.0.5"
    assert extract_target({"targets": ["a.example.com", "b.example.com"]}) == "a.example.com"
    assert extract_target({}) == UNKNOWN_TARGET


def test_bug006_cidr_scope_and_unknown_fail_closed():
    scope = Scope(excluded_targets=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="excluded"):
        check_scope("10.0.0.5", "nmap", scope)
    check_scope("192.168.1.1", "nmap", scope)  # outside the CIDR → allowed
    with pytest.raises(ValueError, match="could not be identified"):
        check_scope(UNKNOWN_TARGET, "nmap", scope)
    # Permissive mode is unchanged.
    check_scope(UNKNOWN_TARGET, "nmap", Scope())


# ── BUG-023 / BUG-024 / BUG-057: process lifecycle + output cap ─────────────


class _BlockingProc:
    """Subprocess stand-in whose communicate() blocks until cancelled."""

    def __init__(self, output=(b"", b"")):
        self.pid = 424242
        self.returncode = None
        self._output = output
        self.ready = asyncio.Event()

    async def communicate(self):
        self.ready.set()
        await asyncio.sleep(60)
        return self._output

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_bug023_shell_cancellation_kills_tree_and_reraises(monkeypatch):
    eng = ExecutionEngine()
    proc = _BlockingProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    kill_mock = AsyncMock()
    monkeypatch.setattr("kmcp.execution.kill_process_tree", kill_mock)

    task = asyncio.create_task(
        eng.execute("sleep 60", tool="generic_command", timeout=5, request_id="cancel-shell")
    )
    await asyncio.wait_for(proc.ready.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kill_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug023_non_shell_cancellation_kills_tree(monkeypatch):
    proc = _BlockingProc()
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/sleep")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    kill_mock = AsyncMock()
    monkeypatch.setattr("kmcp.execution.kill_process_tree", kill_mock)
    registered = []

    task = asyncio.create_task(
        _spawn_isolated_tracked(
            ["sleep", "60"], StepLimits(timeout=5), 5, on_proc=registered.append
        )
    )
    await asyncio.wait_for(proc.ready.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registered == [proc]
    kill_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug024_non_shell_proc_registered_and_killable(monkeypatch):
    eng = ExecutionEngine()
    proc = _BlockingProc()
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/sleep")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    kill_mock = AsyncMock()
    monkeypatch.setattr("kmcp.execution.kill_process_tree", kill_mock)

    task = asyncio.create_task(
        eng.execute(["sleep", "60"], tool="nmap", timeout=5, request_id="kill-nonshell")
    )
    await asyncio.wait_for(proc.ready.wait(), timeout=2)
    assert eng._procs.get("kill-nonshell") is proc
    assert await eng.kill_job("kill-nonshell") is True
    kill_mock.assert_awaited()
    assert eng.active_count == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_bug057_shell_output_truncated_to_max_output(monkeypatch):
    class _OutputProc:
        pid = 424243
        returncode = 0

        async def communicate(self):
            return b"a" * (MAX_OUTPUT + 500), b"b" * (MAX_OUTPUT + 500)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_OutputProc()))
    result = await ExecutionEngine().execute("echo big", timeout=5, request_id="big-output")
    assert len(result.stdout) == MAX_OUTPUT
    assert len(result.stderr) == MAX_OUTPUT


# ── sync kill paths: watchdog / force_cleanup ───────────────────────────────


class _FakeLoop:
    def is_closed(self) -> bool:
        return False


@pytest.mark.parametrize("via_force_cleanup", [True, False])
def test_bug024_sync_cleanup_schedules_process_kill(monkeypatch, via_force_cleanup):
    eng = ExecutionEngine()
    proc = MagicMock()
    eng._active["r"] = "nmap"
    eng._request_times["r"] = 0.0
    eng._procs["r"] = proc
    eng._loop = _FakeLoop()
    scheduled = []

    def _fake_schedule(coro, loop):
        coro.close()  # avoid "coroutine never awaited" warnings
        scheduled.append(loop)
        return MagicMock()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_schedule)
    if via_force_cleanup:
        assert eng.force_cleanup() == 1
    else:
        eng._pop_and_kill("r")  # the watchdog's shared cleanup primitive
    assert scheduled == [eng._loop]
    assert eng._procs == {}
    assert eng.active_count == 0


# ── BUG-025 / BUG-026: profile binding + ContextVar reset ───────────────────


@pytest.mark.asyncio
async def test_bug025_missing_bound_profile_denies_every_call(monkeypatch, cfg_patch):
    cfg_patch(agent_profile_id="ghost", insecure=True)
    # A p1 profile exists and would be picked from headers on the buggy path.
    fake_db = _FakeDB(profiles={"p1"}, grants={("p1", "nmap")})
    monkeypatch.setattr(agent_context, "_db", lambda: fake_db)
    monkeypatch.setenv("MCP_SCOPE_EXCLUDED_TARGETS", "127.0.0.1")

    handler = server_module.mcp._tool_manager._tools["nmap"].fn
    out = await handler(_ctx({agent_context.HEADER_PROFILE: "p1"}), target="127.0.0.1")
    payload = json.loads(out)
    assert payload["forbidden"] is True
    assert payload["profile_id"] == "ghost"


@pytest.mark.asyncio
async def test_bug026_profile_context_reset_on_scope_violation(monkeypatch, cfg_patch):
    cfg_patch(agent_profile_id="", insecure=True)
    fake_db = _FakeDB(profiles={"p1"}, grants={("p1", "nmap")})
    monkeypatch.setattr(agent_context, "_db", lambda: fake_db)
    monkeypatch.setenv("MCP_SCOPE_EXCLUDED_TARGETS", "blocked.example.com")

    handler = server_module.mcp._tool_manager._tools["nmap"].fn
    out = await handler(_ctx({agent_context.HEADER_PROFILE: "p1"}), target="blocked.example.com")
    assert json.loads(out)["scope_violation"] is True
    assert agent_context.get_current_profile() is None


@pytest.mark.asyncio
async def test_bug021_visibility_denies_direct_mcp_call(cfg_patch):
    """A demoted tool must not be callable via MCP even with a grant."""
    cfg_patch(insecure=True)
    from kmcp.registry import registry

    defn = registry.get_definition("nmap")
    original = defn.visibility
    registry.set_visibility("nmap", "internal")
    try:
        handler = server_module.mcp._tool_manager._tools["nmap"].fn
        out = await handler(_ctx({}), target="example.com")
        payload = json.loads(out)
        assert payload["forbidden"] is True
        assert payload["visibility"] == "internal"
    finally:
        registry.set_visibility("nmap", original)


def test_bug021_catalog_excludes_non_agent_callable_tools():
    from kmcp.registry import registry

    defn = registry.get_definition("nmap")
    original = defn.visibility
    registry.set_visibility("nmap", "hidden")
    try:
        names = [t.name for t in registry.visible_tools("agent_callable")]
        assert "nmap" not in names
    finally:
        registry.set_visibility("nmap", original)
