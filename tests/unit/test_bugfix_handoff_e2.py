"""Focused regression tests for the builder handoff (F-08 / F-10 / F-12).

Every collaborator (engine, persistence, PATH/dependency checks) is mocked —
no real subprocess, network call, or sudo invocation happens here.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kmcp.models import ExecutionResult
from kmcp.tools.amass import AmassTool
from kmcp.tools.bettercap import BettercapTool
from kmcp.tools.farsight import FarsightTool
from kmcp.tools.file_download import FileDownloadTool
from kmcp.tools.file_write import FileWriteTool
from kmcp.tools.hashcat import HashcatTool
from kmcp.tools.responder import ResponderTool
from kmcp.tools.spiderfoot import SpiderfootTool
from kmcp.tools.theharvester import TheharvesterTool
from kmcp.tools.zap import ZapTool


def _exec_result(stdout: str = "", stderr: str = "", exit_code: int = 0) -> ExecutionResult:
    return ExecutionResult(
        tool="test",
        command="test",
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        success=exit_code == 0,
        timed_out=False,
        duration=0.0,
        start_time="2026-01-01T00:00:00+00:00",
        end_time="2026-01-01T00:00:00+00:00",
    )


def _mcp_result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": is_error,
    }


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _wrapped(stdout: str = "", stderr: str = "", exit_code: int = 0) -> dict:
    """Shape returned by run_tool_with_persistence (raw + metadata)."""
    return _mcp_result(
        {
            "raw": {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "success": exit_code == 0,
            },
            "normalized": {},
        }
    )


# ── F-08: missing binaries / runtime dependencies ──────────────────────────


async def test_hashcat_missing_binary_is_structured_not_a_crash():
    runner = AsyncMock()
    with (
        patch("kmcp.tools.hashcat.shutil.which", return_value=None),
        patch("kmcp.tools.hashcat.run_tool_with_persistence", runner),
    ):
        result = await HashcatTool().execute({"hash_file": "/tmp/hashes.txt"})

    assert result["isError"] is True
    body = _payload(result)
    assert body["error"] == "Missing dependency"
    details = json.loads(body["details"])
    assert details["missing_binary"] == "hashcat"
    assert "apt install" in details["install_hint"]
    runner.assert_not_awaited()


async def test_zap_missing_binary_names_baseline_script_and_install_hint():
    runner = AsyncMock()
    with (
        patch("kmcp.tools.zap.shutil.which", return_value=None),
        patch("kmcp.tools.zap.run_tool_with_persistence", runner),
    ):
        result = await ZapTool().execute({"target": "http://example.com"})

    assert result["isError"] is True
    body = _payload(result)
    details = json.loads(body["details"])
    assert details["missing_binary"] == "zap-baseline.py"
    assert "zaproxy" in details["install_hint"]
    assert ZapTool().binary == "zap-baseline.py"
    runner.assert_not_awaited()


async def test_responder_missing_netifaces_is_reported_as_dependency_error():
    stderr = (
        "Traceback (most recent call last):\n"
        "  File \"/usr/bin/responder\", line 3, in <module>\n"
        "    import netifaces\n"
        "ModuleNotFoundError: No module named 'netifaces'\n"
    )
    with patch(
        "kmcp.tools.responder.run_tool_with_persistence",
        AsyncMock(return_value=_wrapped(stderr=stderr, exit_code=1)),
    ):
        result = await ResponderTool().execute({"interface": "eth0"})

    assert result["isError"] is True
    body = _payload(result)
    assert body["error"] == "Missing dependency"
    details = json.loads(body["details"])
    assert details["missing_dependency"] == "python3-netifaces"
    assert "python3-netifaces" in details["install_hint"]


async def test_responder_unrelated_stderr_is_not_misclassified():
    with patch(
        "kmcp.tools.responder.run_tool_with_persistence",
        AsyncMock(return_value=_wrapped(stderr="listening on eth0", exit_code=0)),
    ):
        result = await ResponderTool().execute({"interface": "eth0"})

    assert result["isError"] is False
    assert "listening" in _payload(result)["raw"]["stderr"]


async def test_amass_non_root_without_data_errors_without_running_sudo():
    engine_mock = AsyncMock()
    with (
        patch("kmcp.tools.amass.os.geteuid", return_value=1000),
        patch("kmcp.tools.amass._libpostal_data_available", return_value=False),
        patch("kmcp.tools.amass.engine.execute", engine_mock),
    ):
        result = await AmassTool().execute({"domain": "example.com"})

    assert result["isError"] is True
    body = _payload(result)
    assert "root" in body["error"].lower()
    details = json.loads(body["details"])
    assert details["missing_privilege"] == "root"
    assert "sudo" in details["install_hint"]
    engine_mock.assert_not_awaited()


async def test_amass_root_or_seeded_data_still_executes():
    engine_mock = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"stdout": "ok"}))
    with (
        patch("kmcp.tools.amass.os.geteuid", return_value=0),
        patch("kmcp.tools.amass.engine.execute", engine_mock),
    ):
        result = await AmassTool().execute({"domain": "example.com"})

    assert result["isError"] is False
    engine_mock.assert_awaited_once()


async def test_amass_intel_mode_is_not_blocked_by_libpostal_guard():
    engine_mock = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"stdout": "ok"}))
    with (
        patch("kmcp.tools.amass.os.geteuid", return_value=1000),
        patch("kmcp.tools.amass._libpostal_data_available", return_value=False),
        patch("kmcp.tools.amass.engine.execute", engine_mock),
    ):
        result = await AmassTool().execute({"domain": "example.com", "mode": "intel"})

    assert result["isError"] is False
    engine_mock.assert_awaited_once()


async def test_theharvester_missing_proxy_config_skips_proxy_flags(tmp_path):
    missing_config = tmp_path / "proxies.yaml"
    tool = TheharvesterTool()
    args = {
        "domain": "example.com",
        "extra_args": "-b google --proxies -l 100",
    }
    with patch("kmcp.tools.theharvester._PROXY_CONFIG", missing_config):
        cmd = tool.build_command(args)
        assert "--proxies" not in cmd
        assert "google" in cmd  # extra -b overrides the default source
        assert "100" in cmd  # extra -l overrides the default limit

        engine_mock = AsyncMock(return_value=_exec_result())
        with patch("kmcp.tools.theharvester.engine.execute", engine_mock):
            result = await tool.execute(args)

    assert result["isError"] is False
    stdout = _payload(result)["stdout"]
    assert str(missing_config) in stdout
    assert "skipped" in stdout


async def test_theharvester_present_proxy_config_keeps_proxy_flags(tmp_path):
    config_path = tmp_path / "proxies.yaml"
    config_path.write_text("http: []\nsocks5: []\n")
    tool = TheharvesterTool()
    with patch("kmcp.tools.theharvester._PROXY_CONFIG", config_path):
        cmd = tool.build_command(
            {"domain": "example.com", "extra_args": "--proxies"}
        )
    assert "--proxies" in cmd
    assert tool._proxy_skipped is False


def test_spiderfoot_uses_valid_cli_argv_and_start_script():
    tool = SpiderfootTool()
    cmd = tool.build_command({"target": "example.com", "modules": "sfp_dns,sfp_email"})
    path = Path(cmd[2])
    try:
        assert cmd[:2] == ["spiderfoot-cli", "-e"]
        assert "-s" not in cmd and "-m" not in cmd  # not valid top-level sfcli.py flags
        assert path.read_text(encoding="utf-8") == "start example.com -m sfp_dns,sfp_email\n"

        default_cmd = tool.build_command({"target": "example.com"})
        default_path = Path(default_cmd[2])
        try:
            assert default_path.read_text(encoding="utf-8") == "start example.com -u all\n"
        finally:
            default_path.unlink(missing_ok=True)
    finally:
        path.unlink(missing_ok=True)


async def test_spiderfoot_skips_unsupported_extra_flags_and_cleans_temp_file():
    tool = SpiderfootTool()
    engine_mock = AsyncMock(return_value=_exec_result(stdout="scan started"))
    with patch("kmcp.tools.spiderfoot.engine.execute", engine_mock):
        result = await tool.execute(
            {"target": "example.com", "extra_args": "-m sfp_dns -i"}
        )
        cmd = engine_mock.await_args.kwargs["command"]

    assert result["isError"] is False
    assert cmd[:2] == ["spiderfoot-cli", "-e"]
    assert "-m" not in cmd
    assert "-i" in cmd
    assert "skipped" in _payload(result)["stdout"]
    assert not Path(cmd[2]).exists()  # temp commands file cleaned up after run


async def test_farsight_traceback_becomes_structured_error(tmp_path):
    engine_mock = AsyncMock(
        return_value=_exec_result(
            stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'dns'\n",
            exit_code=1,
        )
    )
    with (
        patch("kmcp.tools.farsight.config", SimpleNamespace(artifact_dir=tmp_path)),
        patch("kmcp.tools.farsight.engine.execute", engine_mock),
    ):
        result = await FarsightTool().execute({"domain": "example.com"})

    assert result["isError"] is True
    body = _payload(result)
    assert body["error"] == "Farsight failed"
    details = json.loads(body["details"])
    assert "ModuleNotFoundError" in details["reason"]
    assert details["exit_code"] == 1


async def test_farsight_unexpected_exception_is_structured(tmp_path):
    with (
        patch("kmcp.tools.farsight.config", SimpleNamespace(artifact_dir=tmp_path)),
        patch(
            "kmcp.tools.farsight.engine.execute",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        result = await FarsightTool().execute({"domain": "example.com"})

    assert result["isError"] is True
    body = _payload(result)
    assert body["error"] == "Execution failed"
    assert "RuntimeError: boom" in json.loads(body["details"])["reason"]


async def test_bettercap_surfaces_stderr_and_exit_code():
    with patch(
        "kmcp.tools.bettercap.run_tool_with_persistence",
        AsyncMock(
            return_value=_wrapped(stderr="fatal: no such interface", exit_code=2)
        ),
    ):
        result = await BettercapTool().execute(
            {"target": "192.168.1.0/24", "modules": "net.sniff on"}
        )

    payload = _payload(result)
    assert payload["normalized"]["stderr"] == "fatal: no such interface"
    assert payload["normalized"]["exit_code"] == 2
    assert payload["success"] is False
    assert "fatal" in payload["normalized"]["failure_reason"]


async def test_bettercap_clean_run_stays_successful():
    with patch(
        "kmcp.tools.bettercap.run_tool_with_persistence",
        AsyncMock(return_value=_wrapped(stdout="sniffing", exit_code=0)),
    ):
        result = await BettercapTool().execute(
            {"target": "192.168.1.0/24", "modules": "net.sniff on"}
        )
    payload = _payload(result)
    assert payload.get("success", True) is True
    assert "failure_reason" not in payload["normalized"]
    assert payload["normalized"]["exit_code"] == 0


# ── F-10: no token in printed download command ─────────────────────────────


async def test_file_download_does_not_print_api_token(tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("findings")
    token = "SUPER_SECRET_TOKEN_DO_NOT_LEAK"
    with (
        patch("kmcp.tools.file_download._get_download_host", return_value="127.0.0.1"),
        patch(
            "kmcp.config.config",
            SimpleNamespace(port=8399, api_token=token, artifact_dir=tmp_path),
        ),
    ):
        result = await FileDownloadTool().execute({"server_path": str(path)})

    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert token not in text
    assert "Bearer $KMCP_API_TOKEN" in text
    assert "KMCP_API_TOKEN" in text
    assert str(tmp_path) in text  # artifact_dir restriction is explained
    assert "/download" in text


# ── F-12: append separator is opt-in ───────────────────────────────────────


async def test_file_write_append_legacy_behavior_is_unchanged(tmp_path):
    path = tmp_path / "lines.txt"
    tool = FileWriteTool()
    await tool.execute({"path": str(path), "content": "line1\nline2\n"})
    await tool.execute({"path": str(path), "content": "line3\n", "mode": "append"})
    assert path.read_text() == "line1\nline2\nline3\n"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"separator": "\n"}, "line1\nline2\n\nline3\n"),
        ({"separator": "\\n"}, "line1\nline2\n\nline3\n"),
        ({"newline": True}, "line1\nline2\n\nline3\n"),
        ({"separator": "\n", "newline": True}, "line1\nline2\n\nline3\n"),
    ],
)
async def test_file_write_append_leading_newline_is_opt_in(tmp_path, args, expected):
    path = tmp_path / "lines.txt"
    path.write_text("line1\nline2\n")
    result = await FileWriteTool().execute(
        {"path": str(path), "content": "line3\n", "mode": "append", **args}
    )
    assert result["isError"] is False
    assert path.read_text() == expected


async def test_file_write_separator_does_not_affect_overwrite(tmp_path):
    path = tmp_path / "lines.txt"
    result = await FileWriteTool().execute(
        {"path": str(path), "content": "fresh\n", "separator": "\n"}
    )
    assert result["isError"] is False
    assert path.read_text() == "fresh\n"


async def test_file_write_invalid_separator_is_validation_error(tmp_path):
    path = tmp_path / "lines.txt"
    result = await FileWriteTool().execute(
        {"path": str(path), "content": "x", "mode": "append", "separator": 42}
    )
    assert result["isError"] is True
    assert "separator" in _payload(result)["details"]


def test_file_write_schema_documents_newline_usage():
    props = FileWriteTool().input_schema()["properties"]
    assert props["separator"]["default"] == ""
    assert "newline" in props["newline"]["description"].lower() or "new line" in props[
        "newline"
    ]["description"].lower()
    assert props["newline"]["default"] is False
