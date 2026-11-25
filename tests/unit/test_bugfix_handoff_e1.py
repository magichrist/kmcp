"""F-07 handoff: exact argv regression tests for wrapper argument construction.

Every test here is pure — no subprocess, no network. Only ``build_command`` /
``validate`` (and one validation-only ``execute`` path) are exercised.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kmcp.tools.flowlyt import FlowlytTool
from kmcp.tools.john import JohnTool
from kmcp.tools.masscan import MasscanTool
from kmcp.tools.medusa import MedusaTool
from kmcp.tools.sqlmap import SqlmapTool
from kmcp.tools.wfuzz import WfuzzTool
from kmcp.tools.zizmor import ZizmorTool

# ── masscan ──────────────────────────────────────────────────────────────────


def test_masscan_emits_default_ports_when_omitted():
    assert MasscanTool().build_command({"target": "127.0.0.1"}) == [
        "masscan",
        "127.0.0.1",
        "-p",
        "80,443,8080",
        "--rate",
        "1000",
    ]


def test_masscan_preserves_explicit_ports_and_rate():
    cmd = MasscanTool().build_command(
        {"target": "10.0.0.0/24", "ports": "22,443", "rate": 5000}
    )
    assert cmd == [
        "masscan",
        "10.0.0.0/24",
        "-p",
        "22,443",
        "--rate",
        "5000",
    ]


def test_masscan_accepts_cidr_and_numeric_range():
    assert MasscanTool().build_command({"target": "10.0.0.0/24"})[1] == "10.0.0.0/24"
    assert (
        MasscanTool().build_command({"target": "10.0.0.1-10.0.0.20"})[1]
        == "10.0.0.1-10.0.0.20"
    )


def test_masscan_hostname_rejected_instead_of_invalid_command():
    tool = MasscanTool()
    with pytest.raises(ValueError, match="numeric IP address or CIDR range"):
        tool.validate({"target": "example.com"})
    with pytest.raises(ValueError, match="numeric IP address or CIDR range"):
        tool.build_command({"target": "example.com"})


def test_masscan_hostname_returns_structured_validation_error():
    result = asyncio.run(MasscanTool().execute({"target": "example.com"}))
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "Validation error"
    assert "Resolve the hostname" in payload["details"]


# ── medusa ───────────────────────────────────────────────────────────────────

_MEDUSA_BASE = {
    "service": "ssh",
    "user_list": "/u.txt",
    "pass_list": "/p.txt",
}


def test_medusa_single_host_uses_lowercase_h():
    cmd = MedusaTool().build_command({"target": "127.0.0.1", **_MEDUSA_BASE})
    assert cmd == [
        "medusa",
        "-h",
        "127.0.0.1",
        "-U",
        "/u.txt",
        "-P",
        "/p.txt",
        "-M",
        "ssh",
        "-t",
        "4",
    ]


def test_medusa_host_file_keeps_uppercase_h():
    cmd = MedusaTool().build_command({"target": "/tmp/hosts.txt", **_MEDUSA_BASE})
    assert cmd[:3] == ["medusa", "-H", "/tmp/hosts.txt"]
    assert "-h" not in cmd


def test_medusa_port_still_appended():
    cmd = MedusaTool().build_command(
        {"target": "127.0.0.1", "port": 2222, **_MEDUSA_BASE}
    )
    assert cmd[-2:] == ["-n", "2222"]
    assert cmd[1] == "-h"


# ── john ─────────────────────────────────────────────────────────────────────


def test_john_format_uses_equals_sign():
    assert JohnTool().build_command({"hash_file": "/h", "format": "raw-md5"}) == [
        "john",
        "--format=raw-md5",
        "/h",
    ]


def test_john_format_with_wordlist_and_show():
    cmd = JohnTool().build_command(
        {
            "hash_file": "/h",
            "format": "raw-sha1",
            "wordlist": "/w",
            "show": True,
        }
    )
    assert cmd == ["john", "--format=raw-sha1", "--wordlist", "/w", "--show", "/h"]


# ── zizmor ───────────────────────────────────────────────────────────────────


def test_zizmor_target_first_and_collect_attached(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert ZizmorTool().build_command({"target": "owner/repo"}) == [
        "zizmor",
        "owner/repo",
        "--collect=all",
    ]


def test_zizmor_custom_collect_and_extra_args(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    cmd = ZizmorTool().build_command(
        {
            "target": "/tmp/workflows",
            "collect": "workflows",
            "extra_args": "--offline",
        }
    )
    assert cmd == [
        "zizmor",
        "/tmp/workflows",
        "--collect=workflows",
        "--offline",
    ]
    assert "--collect" not in cmd  # no bare variadic flag swallowing the target


# ── flowlyt ──────────────────────────────────────────────────────────────────


def test_flowlyt_local_repo_uses_repo_flag():
    assert FlowlytTool().build_command({"repo": "/tmp/myrepo"}) == [
        "flowlyt",
        "scan",
        "--repo",
        "/tmp/myrepo",
    ]


def test_flowlyt_url_and_slug_use_url_flag():
    assert FlowlytTool().build_command(
        {"repo": "https://github.com/owner/repo"}
    ) == ["flowlyt", "scan", "--url", "https://github.com/owner/repo"]
    assert FlowlytTool().build_command({"repo": "owner/repo"}) == [
        "flowlyt",
        "scan",
        "--url",
        "owner/repo",
    ]


def test_flowlyt_workflow_file_with_explicit_repo():
    cmd = FlowlytTool().build_command(
        {"repo": ".", "workflow_file": ".github/workflows/ci.yml"}
    )
    assert cmd == [
        "flowlyt",
        "scan",
        "--repo",
        ".",
        "--file",
        ".github/workflows/ci.yml",
    ]
    assert cmd.count(".") == 1  # repo is not emitted as a bare positional arg


# ── wfuzz ────────────────────────────────────────────────────────────────────

_WFUZZ_ARGS = {"target": "http://x/FUZZ", "wordlist": "/w"}


@pytest.mark.parametrize(
    "filters",
    ["200", "--filter 200", "--filter=200", ["--filter", "200"], ["200"]],
)
def test_wfuzz_emits_filter_exactly_once(filters):
    cmd = WfuzzTool().build_command({**_WFUZZ_ARGS, "filters": filters})
    assert cmd == ["wfuzz", "-w", "/w", "http://x/FUZZ", "--filter", "200"]
    assert cmd.count("--filter") == 1


def test_wfuzz_preserves_quoted_filter_expression():
    cmd = WfuzzTool().build_command(
        {**_WFUZZ_ARGS, "filters": "--filter 'c=200 and c=404'"}
    )
    assert cmd == [
        "wfuzz",
        "-w",
        "/w",
        "http://x/FUZZ",
        "--filter",
        "c=200 and c=404",
    ]


# ── sqlmap ───────────────────────────────────────────────────────────────────


def test_sqlmap_simple_batch_command():
    assert SqlmapTool().build_command({"target": "http://x"}) == [
        "sqlmap",
        "-u",
        "http://x",
        "--batch",
    ]


def test_sqlmap_normalizes_level_risk_output_dir():
    cmd = SqlmapTool().build_command(
        {
            "target": "http://x",
            "extra_args": "--level 5 --risk 3 --output-dir /tmp/sqlmap-out",
        }
    )
    assert cmd == [
        "sqlmap",
        "-u",
        "http://x",
        "--level=5",
        "--risk=3",
        "--output-dir=/tmp/sqlmap-out",
        "--batch",
    ]


def test_sqlmap_accepts_preformatted_and_list_extra_args():
    expected = [
        "sqlmap",
        "-u",
        "http://x",
        "--level=5",
        "--risk=2",
        "--batch",
    ]
    assert (
        SqlmapTool().build_command(
            {"target": "http://x", "extra_args": "--level=5 --risk=2"}
        )
        == expected
    )
    assert (
        SqlmapTool().build_command(
            {"target": "http://x", "extra_args": ["--level", "5", "--risk", "2"]}
        )
        == expected
    )


def test_sqlmap_batch_deduped_and_url_override_dropped():
    cmd = SqlmapTool().build_command(
        {
            "target": "http://x",
            "extra_args": "--batch -u http://evil --dbs --batch",
        }
    )
    assert cmd == ["sqlmap", "-u", "http://x", "--dbs", "--batch"]
    assert cmd.count("--batch") == 1
