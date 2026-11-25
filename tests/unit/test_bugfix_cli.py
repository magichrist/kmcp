"""Regression tests for verified CLI/eval/monitor/coverage bug fixes.

Focused, isolated tests: mocks and tmp dirs only — no real server process and
no destructive operations against repository data paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from kmcp.eval.harness import (
    AssessmentResult,
    CallRecord,
    EvalHarness,
    EvalSuite,
    GroundTruthFinding,
    _extract_finding_titles,
)
from kmcp.intelligence import CoverageTracker
from kmcp.monitor import sse_client
from kmcp.monitor.tui import PANEL_JOBS, _http_post, _kill_selected, _status_after_snapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _result(
    model: str,
    interface: str,
    *,
    calls: int = 1,
    invalid: int = 0,
    completed: bool = True,
    precision: float = 0.0,
    recall: float = 0.0,
) -> AssessmentResult:
    """Build an AssessmentResult with the derived metrics already set."""
    now = _now()
    result = AssessmentResult(
        run_id=f"{model}_{interface}",
        model_name=model,
        interface=interface,
        target="lab",
        start_time=now,
        end_time=now,
        duration_s=1.0,
        completed=completed,
    )
    result.mcp_calls = calls
    result.invalid_calls = invalid
    result.precision = precision
    result.recall = recall
    result.f1 = 0.0
    return result


# ── BUG-002: result serialization ─────────────────────────────────────────


def test_write_result_serializes_assessment_result(tmp_path: Path) -> None:
    suite = EvalSuite(output_dir=tmp_path)
    result = _result("model-a", "v2")
    # Previously raised AttributeError: AssessmentResult has no to_dict().
    suite._write_result(result)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["run_id"] == result.run_id
    assert payload["mcp_calls"] == 1


# ── BUG-014: token metrics ─────────────────────────────────────────────────


async def test_token_metrics_filled_before_compute(monkeypatch) -> None:
    call = CallRecord(
        call_id="c1",
        timestamp=_now(),
        tool="nmap",
        arguments={"target": "example.com"},
        duration_s=1.0,
        success=True,
    )

    async def fake_v2(self, run_id, target, workflow, calls, max_calls, timeout_s):
        return [], [call]

    monkeypatch.setattr(EvalHarness, "_run_v2", fake_v2)
    result = await EvalHarness(model_name="m", interface="v2").run_assessment(target="example.com")
    assert result.total_input_tokens > 0
    assert result.total_output_tokens > 0
    assert result.total_output_tokens == call.output_tokens


# ── BUG-015: v1 finding accuracy ───────────────────────────────────────────


def test_derive_finding_titles_from_tool_output() -> None:
    gt = [
        GroundTruthFinding(
            title="Open HTTP port 80",
            severity="informational",
            asset="example.com",
            port=80,
            service="http",
            source_tool="nmap",
        )
    ]
    derived = _extract_finding_titles(
        "Starting Nmap\n80/tcp open http\nDirectory Listing: enabled\n", gt
    )
    titles = {meta["title"] for meta in derived.values()}
    assert "Open HTTP port 80" in titles
    assert "Directory listing enabled" in titles
    http = next(meta for meta in derived.values() if meta["title"] == "Open HTTP port 80")
    assert http["port"] == 80
    assert http["service"] == "http"


def test_metrics_match_normalized_title_asset_and_port() -> None:
    gt = [
        GroundTruthFinding(
            title="Open HTTP port 80",
            severity="informational",
            asset="10.0.0.1",
            port=80,
            service="http",
            source_tool="nmap",
        )
    ]

    def _run(findings):
        result = _result("m", "v1")
        result.findings = findings
        result.compute_metrics(gt)
        return result

    matching = _run(
        [{"title": "open http port 80!", "asset": "http://10.0.0.1/", "port": 80, "tool": "nmap"}]
    )
    assert (matching.precision, matching.recall, matching.f1) == (1.0, 1.0, 1.0)

    wrong_port = _run([{"title": "Open HTTP port 80", "asset": "10.0.0.1", "port": 443}])
    assert wrong_port.recall == 0.0
    assert wrong_port.precision == 0.0

    wrong_asset = _run([{"title": "Open HTTP port 80", "asset": "10.0.0.2", "port": 80}])
    assert wrong_asset.recall == 0.0


async def test_v1_run_derives_real_titles_instead_of_placeholder(monkeypatch) -> None:
    from kmcp.eval import harness as harness_mod

    class FakeTool:
        async def safe_execute(self, args):
            return {
                "isError": False,
                "content": [{"text": json.dumps({"stdout": "80/tcp open http"})}],
            }

    monkeypatch.setattr(
        "kmcp.registry.registry.get", lambda name: FakeTool() if name == "nmap" else None
    )
    monkeypatch.setattr(harness_mod.asyncio, "sleep", AsyncMock())

    harness = EvalHarness(model_name="m", interface="v1")
    findings, _calls = await harness._run_v1("run", "example.com", [], 5, 10)
    assert findings
    assert all(f["title"] != "nmap finding" for f in findings)
    assert any(f["title"] == "Open HTTP port 80" for f in findings)
    assert all(f.get("tool") == "nmap" for f in findings)


# ── BUG-038: aggregate across every model group ────────────────────────────


def test_aggregate_report_comparison_uses_all_groups(tmp_path: Path) -> None:
    suite = EvalSuite(output_dir=tmp_path)
    results = [
        _result("model-a", "v1", calls=10),
        _result("model-b", "v1", calls=10),
        _result("model-a", "v2", calls=1),
        _result("model-b", "v2", calls=5),
    ]
    suite._write_aggregate_report(results)
    report = json.loads((tmp_path / "aggregate_report.json").read_text())
    # v1 avg 10 calls, v2 avg 3 calls → 70% reduction (first-group-only was 90%).
    assert report["comparison"]["calls_reduction_percent"] == 70.0


def test_success_criteria_use_all_groups(tmp_path: Path) -> None:
    suite = EvalSuite(output_dir=tmp_path)
    results = [
        _result("model-a", "v1", calls=10),
        _result("model-b", "v1", calls=10),
        _result("model-a", "v2", calls=1),
        _result("model-b", "v2", calls=1, invalid=2),
    ]
    criteria = suite.check_success_criteria(results)
    # The second v2 group regressed; checking only the first group missed it.
    assert criteria["invalid_calls_no_increase"] is False


# ── BUG-016: TUI actions carry the bearer token ────────────────────────────


async def test_http_post_sends_authorization_header(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"killed": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("kmcp.monitor.tui.httpx.AsyncClient", FakeClient)
    headers = {"Authorization": "Bearer secret"}
    result = await _http_post("http://server", {"action": "force_cleanup"}, headers=headers)
    assert result == {"killed": True}
    assert captured["client_kwargs"]["headers"] == headers
    assert captured["url"] == "http://server/monitor/action"


async def test_kill_selected_forwards_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(base_url, payload, headers=None):
        captured.update(url=base_url, payload=payload, headers=headers)
        return {"killed": True}

    monkeypatch.setattr("kmcp.monitor.tui._http_post", fake_post)
    snapshot = {"jobs": [{"request_id": "req-1"}]}
    headers = {"Authorization": "Bearer secret"}
    message = await _kill_selected("http://server", PANEL_JOBS, 0, snapshot, headers=headers)
    assert "Killed job req-1" in message
    assert captured["headers"] == headers
    assert captured["payload"] == {"action": "kill_job", "request_id": "req-1"}


# ── BUG-039: SSE reconnect on protocol errors ──────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [httpx.RemoteProtocolError("peer closed"), httpx.ReadTimeout("timed out")],
)
async def test_sse_client_reconnects_on_http_error(monkeypatch, exc) -> None:
    client = sse_client.SseClient("http://server/monitor/stream")

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise exc

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FailingClient)
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        client._closed = True  # stop after a single retry in the test

    monkeypatch.setattr(sse_client.asyncio, "sleep", fake_sleep)
    await client._read_loop()
    assert sleeps == [2.0]
    assert client.last_error and "Stream error" in client.last_error


# ── BUG-058: connection-error status resets on recovery ────────────────────


def test_connection_error_clears_after_recovery() -> None:
    stale = "Connection error: refused · q: quit · r: retry"
    assert _status_after_snapshot(stale, None).startswith("q: quit")
    assert _status_after_snapshot(stale, "still down") == stale


# ── BUG-017: Justfile recipes dispatch to the start subcommand ────────────


def test_justfile_start_recipes_start_the_server() -> None:
    justfile = Path(__file__).resolve().parents[2] / "Justfile"
    lines = justfile.read_text().splitlines()
    for recipe in ("start:", "debug:"):
        index = lines.index(recipe)
        body = lines[index + 1].strip()
        assert "python -m kmcp start" in body
    assert any("MCP_DEBUG=true python -m kmcp start" in line for line in lines)


# ── BUG-040 / BUG-041: studio port handling ────────────────────────────────


def test_studio_port_flag_is_exported_to_child(monkeypatch) -> None:
    from kmcp import cli

    monkeypatch.setenv("MCP_PORT", "8399")
    monkeypatch.delenv("STUDIO_PORT", raising=False)
    monkeypatch.setattr(cli, "_server_reachable", lambda url, timeout=1.5: False)
    monkeypatch.setattr(cli, "_wait_for_server", lambda urls, proc, timeout=25.0: True)

    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def poll(self):
            return None

    def fake_popen(cmd, env=None, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    args = SimpleNamespace(url=None, host=None, port="8340", no_browser=True)
    assert cli.cmd_studio(args) == 0
    assert captured["env"]["STUDIO_PORT"] == "8340"


def test_studio_url_without_port_uses_fallback(monkeypatch) -> None:
    from kmcp import cli

    monkeypatch.setenv("MCP_PORT", "9002")
    monkeypatch.delenv("STUDIO_PORT", raising=False)
    reached: list[str] = []
    monkeypatch.setattr(
        cli, "_server_reachable", lambda url, timeout=1.5: reached.append(url) or True
    )
    args = SimpleNamespace(url="http://example.com", host=None, port=None, no_browser=True)
    assert cli.cmd_studio(args) == 0
    assert any(":9002/studio" in url for url in reached)


def test_server_reachable_handles_invalid_url() -> None:
    from kmcp.cli import _server_reachable

    # InvalidURL is not a subclass of httpx.HTTPError; this used to propagate.
    assert _server_reachable("http://127.0.0.1:None/studio") is False


# ── BUG-042: purge resolves configured paths ───────────────────────────────


def test_purge_uses_config_paths_not_repo_root(monkeypatch, tmp_path: Path) -> None:
    from kmcp import cli

    root = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    (root / "data").mkdir(parents=True)
    decoy = root / "data" / "kmcp.sqlite"
    decoy.write_text("must survive")

    db = runtime / "kmcp.sqlite"
    wal = runtime / "kmcp.sqlite-wal"
    vault = runtime / "artifacts" / "vault"
    db.parent.mkdir(parents=True)
    db.write_text("db")
    wal.write_text("wal")
    vault.mkdir(parents=True)
    (vault / "index.json").write_text("{}")

    monkeypatch.setattr(cli, "REPO_ROOT", root)
    monkeypatch.setattr(
        "kmcp.config.config",
        SimpleNamespace(db_path=db, artifact_dir=runtime / "artifacts"),
    )
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)

    assert cli.cmd_purge(SimpleNamespace(scope="all", confirm=True)) == 0
    assert not db.exists()
    assert not wal.exists()
    assert not vault.exists()
    assert decoy.exists()  # REPO_ROOT path is no longer the purge target


# ── BUG-043: init writes a secure .env in the project directory ────────────


def test_init_writes_cwd_env_with_secure_permissions(monkeypatch, tmp_path: Path) -> None:
    from kmcp import cli

    elsewhere = tmp_path / "site-packages-like"
    elsewhere.mkdir()
    monkeypatch.setattr(cli, "REPO_ROOT", elsewhere)
    monkeypatch.setattr(cli, "_DEFAULT_REPO_ROOT", elsewhere)  # default root → cwd
    monkeypatch.chdir(tmp_path)

    assert cli.cmd_init(SimpleNamespace(force=False, env_path=None)) == 0
    env = tmp_path / ".env"
    assert env.exists()
    assert env.stat().st_mode & 0o777 == 0o600
    assert not (elsewhere / ".env").exists()

    # --force backs the file up as .env.bak (not .env.env.bak).
    env.write_text("MCP_API_TOKEN=old\n")
    assert cli.cmd_init(SimpleNamespace(force=True, env_path=None)) == 0
    assert (tmp_path / ".env.bak").exists()
    assert not (tmp_path / ".env.env.bak").exists()

    # Explicit path support.
    target = tmp_path / "custom.env"
    assert cli.cmd_init(SimpleNamespace(force=False, env_path=str(target))) == 0
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600

    from kmcp.cli import build_parser

    assert build_parser().parse_args(["init", "--path", "custom.env"]).env_path == "custom.env"


# ── BUG-044: coverage discovery registration ──────────────────────────────


def test_discovered_assets_and_services_produce_gaps() -> None:
    tracker = CoverageTracker()
    tracker.register_asset("assess", "host-1")
    tracker.register_service("assess", "host-1", "http/443")
    tracker.record_coverage("assess", "host-2", None, "nmap", "scanned", 0)

    summary = tracker.get_coverage_summary("assess")
    assert summary["assets_discovered"] == 2
    assert summary["assets_scanned"] == 1
    assert summary["percent_covered"] == 50.0

    gaps = tracker.get_coverage_gaps("assess")
    assert any(g["type"] == "asset_unscanned" and g["asset_id"] == "host-1" for g in gaps)
    assert any(g["type"] == "service_unscanned" and g["service"] == "http/443" for g in gaps)
