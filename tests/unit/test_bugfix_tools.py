"""Focused regression tests for tool/playbook bug fixes.

Covers BUG-018, BUG-019, BUG-022, BUG-050..055, BUG-060, BUG-061.
No real subprocess/network/database access: all collaborators are mocked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kmcp.playbooks.context import AssessmentContext
from kmcp.playbooks.engine import _UNRESOLVED, PlaybookEngine
from kmcp.tools.gobuster import GobusterTool
from kmcp.tools.hashcat import HashcatTool
from kmcp.tools.parallel_execute import ParallelExecuteTool, aggregate_results
from kmcp.tools.playbook_blackbox import JOB_REGISTRY, PlaybookRunTool, _map_flat_target
from kmcp.tools.template_tools import TemplateRunTool, _extract_params


@pytest.fixture(autouse=True)
def _clear_job_registry():
    JOB_REGISTRY.clear()
    yield
    JOB_REGISTRY.clear()


def _ctx(**overrides):
    kwargs = {
        "assessment_id": "a1",
        "job_id": "j1",
        "playbook_id": "pb",
        "target": "example.com",
        "scope_id": "",
        "profile": "safe_active",
        "detail": "normal",
        "inputs": {"target": "example.com", "tier": "quick", "options": {"ports": "80,443"}},
    }
    kwargs.update(overrides)
    return AssessmentContext(**kwargs)


def _text_payload(result: dict) -> dict:
    return json.loads(json.loads(result["content"][0]["text"])["stdout"])


def _sub_result(tool: str, stdout: str) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "tool": tool,
                        "command": tool,
                        "stdout": stdout,
                        "stderr": "",
                        "success": True,
                    }
                ),
            }
        ],
        "isError": False,
    }


# ── BUG-018: args + placeholder expansion ────────────────────────────────


def test_resolve_inputs_reads_args_and_expands_placeholders():
    engine = PlaybookEngine()
    ctx = _ctx()
    step = {
        "args": {
            "target": "{{target}}",
            "ports": "{{inputs.options.ports}}",
            "nested": {"host": "{{target}}", "list": ["{{inputs.tier}}"]},
        },
        "inputs": {"depth": "{{inputs.tier}}"},
    }
    resolved = engine._resolve_inputs({}, step, ctx)
    assert resolved["target"] == "example.com"
    assert resolved["ports"] == "80,443"
    assert resolved["nested"] == {"host": "example.com", "list": ["quick"]}
    assert resolved["depth"] == "quick"
    assert resolved["_assessment_id"] == "a1"


def test_effective_inputs_seed_defaults_and_supplied_values():
    pb_def = {
        "inputs": {
            "tier": {"default": "quick"},
            "options": {
                "properties": {
                    "ports": {"default": "1-1000"},
                    "rate": {"default": "500"},
                }
            },
        }
    }
    resolved = PlaybookEngine._effective_inputs(
        pb_def, {"tier": "big", "options": {"rate": "1000"}}, None, "host.example"
    )
    assert resolved["tier"] == "big"
    assert resolved["options"]["ports"] == "1-1000"
    assert resolved["options"]["rate"] == "1000"
    assert resolved["target"] == "host.example"


# ── BUG-022: inputs/steps refs, foreach, conditions ────────────────────────


def test_resolve_ref_supports_inputs_steps_and_unresolved_sentinel():
    engine = PlaybookEngine()
    ctx = _ctx()
    ctx.set_module_result("scan", {"ports": [80, 443]})
    assert engine._resolve_ref("inputs.tier", ctx) == "quick"
    assert engine._resolve_ref("options.ports", ctx) == "80,443"  # bare input root
    assert engine._resolve_ref("steps.scan.ports", ctx) == [80, 443]
    assert engine._resolve_ref("target", ctx) == "example.com"
    assert engine._resolve_ref("inputs.does_not_exist", ctx) is _UNRESOLVED


def test_foreach_and_when_use_inputs_refs():
    engine = PlaybookEngine()
    ctx = _ctx(inputs={"target": "example.com", "hosts": ["a", "b"], "tier": "quick"})
    assert engine._resolve_foreach("{{inputs.hosts}}", ctx) == ["a", "b"]
    assert engine._evaluate_condition({"when": "inputs.tier == quick"}, ctx) is True
    assert engine._evaluate_condition({"when": "inputs.tier != quick"}, ctx) is False

    other = _ctx(inputs={"target": "example.com", "tier": "big"})
    assert engine._evaluate_condition({"when": "inputs.tier != quick"}, other) is True
    # Unresolved refs are explicit: no iteration, condition does not pass.
    assert engine._resolve_foreach("inputs.missing", ctx) == []
    assert engine._evaluate_condition({"when": "inputs.missing == quick"}, ctx) is False


def test_context_to_dict_derives_status_and_counts():
    ctx = _ctx()
    ctx.set_module_result("scan", {"ok": True})
    ctx.log("scan", "completed", "info")
    ctx.log("dns", "error: binary missing", "error")
    ctx.log("tls", "skipped: condition not met", "debug")
    data = ctx.to_dict()
    assert data["execution"]["completed"] == 1
    assert data["execution"]["failed"] == 1
    assert data["execution"]["skipped"] == 1
    assert data["execution"]["status"] == "partial"
    assert data["status"] == "partial"

    failed_only = _ctx()
    failed_only.log("scan", "error: boom", "error")
    assert failed_only.to_dict()["execution"]["status"] == "failed"


# ── BUG-051: numeric comparison guard ─────────────────────────────────────


def test_compare_numeric_and_non_numeric_without_raising():
    assert PlaybookEngine._compare("10", ">", "2") is True
    assert PlaybookEngine._compare(3, "<=", 3) is True
    # A string-valued field must not abort the playbook with ValueError.
    assert PlaybookEngine._compare("abc", ">", 1) in (True, False)
    assert PlaybookEngine._compare(None, ">", 1) is False
    assert PlaybookEngine._compare(_UNRESOLVED, ">", 1) is False


# ── BUG-061: cyclic dependency error ──────────────────────────────────────


def test_topological_sort_raises_on_cycle():
    engine = PlaybookEngine()
    steps = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="cyclic depends_on"):
        engine._topological_sort(steps)


# ── BUG-019: flat target mapping ──────────────────────────────────────────


def test_extract_params_maps_first_target_like_key():
    args = {"template_id": "quick_web_scan", "url": "http://example.com", "depth": "deep"}
    params = _extract_params(args, declared={"target", "depth"})
    assert params["target"] == "http://example.com"
    assert params["depth"] == "deep"
    assert "url" not in params
    # Templates that do not declare target are left alone.
    assert "target" not in _extract_params(args, declared={"depth"})


def test_map_flat_target_prefers_canonical_and_existing_params():
    assert _map_flat_target({"host": "h", "target": "t"}, {}, {"target"})["target"] == "t"
    assert _map_flat_target({"host": "h"}, {"target": "given"}, {"target"})["target"] == "given"


async def test_template_run_forwards_flat_target_to_renderer():
    captured: dict = {}

    async def fake_run_defn(template_id, params, job_id, assessment_id, tool_name):
        captured.update(params)
        JOB_REGISTRY[job_id] = {"job_id": job_id, "status": "running"}

        async def run():
            return SimpleNamespace(status="completed", stdout="{}")

        return job_id, assessment_id, run

    with (
        patch("kmcp.tools.template_tools._run_template_defn", side_effect=fake_run_defn),
        patch(
            "kmcp.playbooks.registry.get_template",
            return_value={"parameters": {"target": {"required": True}}},
        ),
    ):
        await TemplateRunTool().execute({"template_id": "quick_web_scan", "host": "host.test"})

    assert captured["target"] == "host.test"


async def test_playbook_run_template_forwards_flat_target_to_renderer():
    captured: dict = {}

    async def fake_run_defn(template_id, params, job_id, assessment_id, tool_name):
        captured.update(params)
        JOB_REGISTRY[job_id] = {"job_id": job_id, "status": "running"}

        async def run():
            return SimpleNamespace(status="completed", stdout="{}")

        return job_id, assessment_id, run

    with (
        patch("kmcp.tools.playbook_blackbox._run_template_defn", side_effect=fake_run_defn),
        patch(
            "kmcp.playbooks.registry.get_template",
            return_value={"parameters": {"target": {"required": True}}},
        ),
    ):
        await PlaybookRunTool().execute({"template": "quick_web_scan", "url": "http://url.test"})

    assert captured["target"] == "http://url.test"


# ── BUG-050: parallel_execute target injection + dedup ─────────────────────


async def test_parallel_execute_injects_target_into_subtools():
    seen: list[dict] = []

    class FakeTool:
        async def safe_execute(self, args: dict):
            seen.append(dict(args))
            return _sub_result("nmap", "")

    registry = MagicMock()
    registry.get.return_value = FakeTool()
    with patch("kmcp.registry.registry", registry):
        result = await ParallelExecuteTool().execute(
            {
                "target": "10.0.0.1",
                "tools": [
                    {"tool": "nmap", "args": {"ports": "80"}},
                    {"tool": "ffuf", "args": {"target": "override", "wordlist": "/w"}},
                ],
            }
        )
    assert result["isError"] is False
    assert seen[0]["target"] == "10.0.0.1"
    assert seen[1]["target"] == "override"


def test_aggregate_results_dedups_on_target():
    finding = "Discovered open port 80/tcp on 10.0.0.1"
    results = [
        _sub_result("nmap", finding),
        _sub_result("masscan", finding),
    ]
    assert len(aggregate_results(results, target="10.0.0.1")["open_ports"]) == 1
    # Without a shared target, the neutral fallback still dedups the same
    # port reported by two tools (tool name is not a target).
    assert len(aggregate_results(results, target="")["open_ports"]) == 1
    assert (
        len(
            aggregate_results(
                [_sub_result("nmap", finding), _sub_result("nmap", "open port 80/tcp")],
                target="10.0.0.1",
            )["open_ports"]
        )
        == 1
    )


# ── BUG-053/054/055: tool command construction ────────────────────────────


async def test_gobuster_modes_use_mode_specific_target_flags():
    assert GobusterTool().build_command({"target": "example.com", "mode": "dns", "wordlist": "/w"})[
        :4
    ] == ["gobuster", "dns", "-d", "example.com"]
    assert GobusterTool().build_command(
        {"target": "example.com", "mode": "vhost", "wordlist": "/w"}
    )[:4] == ["gobuster", "vhost", "-v", "example.com"]
    assert GobusterTool().build_command({"target": "http://x", "mode": "dir", "wordlist": "/w"})[
        :4
    ] == ["gobuster", "dir", "-u", "http://x"]


def test_hashcat_puts_hash_file_before_wordlist():
    cmd = HashcatTool().build_command({"hash_file": "/hashes", "wordlist": "/words", "mode": 0})
    assert cmd[0] == "hashcat"
    assert cmd.index("/hashes") < cmd.index("/words")
    assert cmd[-2:] == ["/hashes", "/words"]


async def test_file_download_percent_encodes_query_path(tmp_path):
    from kmcp.tools.file_download import FileDownloadTool

    path = tmp_path / "my file's&report.txt"
    path.write_text("data")
    with (
        patch("kmcp.tools.file_download._get_download_host", return_value="127.0.0.1"),
        patch("kmcp.config.config", SimpleNamespace(port=8399, api_token="")),
    ):
        result = await FileDownloadTool().execute({"server_path": str(path)})
    assert result["isError"] is False
    text = json.loads(result["content"][0]["text"])["stdout"]
    assert "path=" in text
    assert "%20" in text and "%27" in text and "%26" in text
    assert "curl -sS -o" in text
    assert f"'{path}'" not in text  # shell quoting is not URL encoding


# ── BUG-060: byte-accurate file reads ──────────────────────────────────────


async def test_file_read_counts_bytes_not_characters(tmp_path):
    from kmcp.tools.file_read import FileReadTool

    path = tmp_path / "unicode.txt"
    path.write_bytes("ééééé".encode())  # 10 bytes, 5 characters
    result = await FileReadTool().execute({"path": str(path), "max_bytes": 4})
    assert result["isError"] is False
    inner = _text_payload(result)
    assert inner["bytes_read"] == 4
    assert inner["content"] == "éé"
    assert inner["truncated"] is True
