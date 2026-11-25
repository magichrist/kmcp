"""Regression tests for post-fix retest defects R-03 (wfuzz) and R-04 (zizmor).

R-03: wfuzz rejects options placed after the positional URL
      ("Fatal exception: Too many arguments"). Options must precede the target.
R-04: zizmor auto-reads GH_TOKEN from the inherited environment and rejects an
      empty value ("GitHub token cannot be empty"). Empty credential variables
      must be stripped from the env handed to the execution engine.

All collaborator calls are mocked — no real subprocess or network is used.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kmcp.models import ExecutionResult
from kmcp.tools.wfuzz import WfuzzTool
from kmcp.tools.zizmor import ZizmorTool

_TARGET = "http://127.0.0.1:8000/FUZZ"
_WORDLIST = "/usr/share/wordlists/dirb/common.txt"


def _exec_result() -> ExecutionResult:
    return ExecutionResult(
        tool="test",
        command="test",
        stdout="",
        stderr="",
        exit_code=0,
        success=True,
        timed_out=False,
        duration=0.0,
        start_time="2026-01-01T00:00:00+00:00",
        end_time="2026-01-01T00:00:00+00:00",
    )


# ── R-03: wfuzz option placement ─────────────────────────────────────────────


def test_wfuzz_filter_precedes_target():
    cmd = WfuzzTool().build_command({"target": _TARGET, "wordlist": "/w", "filters": "404"})
    assert cmd == ["wfuzz", "-w", "/w", "--filter", "404", _TARGET]
    assert cmd[-1] == _TARGET  # target is the final positional argument


@pytest.mark.parametrize(
    "filters",
    ["404", "--filter 404", "--filter=404", ["--filter", "404"], ["404"]],
)
def test_wfuzz_normalizes_filter_forms_before_target(filters):
    cmd = WfuzzTool().build_command({"target": _TARGET, "wordlist": "/w", "filters": filters})
    assert cmd == ["wfuzz", "-w", "/w", "--filter", "404", _TARGET]
    assert cmd.count("--filter") == 1


def test_wfuzz_method_and_filter_order_with_target_last():
    cmd = WfuzzTool().build_command(
        {
            "target": _TARGET,
            "wordlist": "/w",
            "methods": "POST",
            "filters": "--filter=404",
        }
    )
    assert cmd == ["wfuzz", "-w", "/w", "-X", "POST", "--filter", "404", _TARGET]
    assert cmd[-1] == _TARGET


def test_wfuzz_no_options_keeps_target_last_with_default_wordlist():
    cmd = WfuzzTool().build_command({"target": _TARGET})
    assert cmd == ["wfuzz", "-w", _WORDLIST, _TARGET]
    assert cmd[-1] == _TARGET


def test_wfuzz_requires_fuzz_placeholder():
    with pytest.raises(ValueError, match="FUZZ"):
        WfuzzTool().validate({"target": "http://127.0.0.1:8000/no-placeholder"})


# ── R-04: zizmor empty GH_TOKEN environment ──────────────────────────────────


def test_zizmor_build_command_omits_flag_for_empty_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "")
    cmd = ZizmorTool().build_command({"target": "/tmp/demo.yml"})
    assert cmd == ["zizmor", "/tmp/demo.yml", "--collect=all"]
    assert "--gh-token" not in cmd


def test_zizmor_build_command_includes_flag_for_nonempty_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret123")
    cmd = ZizmorTool().build_command({"target": "/tmp/demo.yml"})
    assert cmd == [
        "zizmor",
        "/tmp/demo.yml",
        "--collect=all",
        "--gh-token",
        "ghp_secret123",
    ]


async def test_zizmor_execute_strips_empty_gh_token_from_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.setenv("KEEP_ME", "preserved")
    mock_execute = AsyncMock(return_value=_exec_result())

    with patch("kmcp.tools.zizmor.engine.execute", mock_execute):
        result = await ZizmorTool().execute({"target": "/tmp/demo.yml"})

    assert result["isError"] is False
    cmd = mock_execute.call_args.args[0]
    assert "--gh-token" not in cmd

    env = mock_execute.call_args.kwargs["env"]
    assert "GH_TOKEN" not in env
    assert env["KEEP_ME"] == "preserved"
    assert env is not os.environ  # engine receives a mutable copy


async def test_zizmor_execute_passes_nonempty_token_as_flag(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret123")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_execute = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"stdout": "ok"}))

    with patch("kmcp.tools.zizmor.engine.execute", mock_execute):
        result = await ZizmorTool().execute({"target": "/tmp/demo.yml"})

    assert result["isError"] is False
    cmd = mock_execute.call_args.args[0]
    assert cmd[cmd.index("--gh-token") + 1] == "ghp_secret123"
    env = mock_execute.call_args.kwargs["env"]
    assert env.get("GH_TOKEN") == "ghp_secret123"


async def test_zizmor_execute_strips_other_empty_credential_vars(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "")

    mock_execute = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"stdout": "ok"}))

    with patch("kmcp.tools.zizmor.engine.execute", mock_execute):
        await ZizmorTool().execute({"target": "/tmp/demo.yml"})

    env = mock_execute.call_args.kwargs["env"]
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
