"""Regression tests for verified workflow bugs (BUG-009/010/011/012/013/033/034/035/036/037/062).

Each test reproduces the defect described in ``docs/bugs.md`` against the real
engine/validator/DSL code with mocked registry, database and scope dependencies.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kmcp.workflow import dag_executor as de
from kmcp.workflow.dag_executor import PlaybookEngine
from kmcp.workflow.dsl import compile_dsl, deserialize
from kmcp.workflow.scope_gate import ScopeViolationError, extract_targets
from kmcp.workflow.validator import is_valid, validate_playbook
from kmcp.workflow.yaml_loader import PlaybookDefinition, StepDefinition


def _pb(name="pb", steps=None, inputs=None, execution=None):
    return PlaybookDefinition(
        name=name,
        version="1.0",
        inputs=inputs or {},
        execution=execution or {"max_parallel": 4, "timeout": 900},
        steps=steps or [],
        raw={},
    )


def _step(id="s", tool="nmap", args=None, **kwargs):
    return StepDefinition(id=id, tool=tool, args=dict(args or {}), **kwargs)


def _tool(result=None, call_log=None):
    async def _safe_execute(args):
        if call_log is not None:
            call_log.append(dict(args))
        return result if result is not None else {"ok": True}

    tool = AsyncMock()
    tool.safe_execute = _safe_execute
    return tool


def _registry(tools):
    reg = MagicMock()
    reg.get.side_effect = lambda name: tools.get(name)
    return reg


@pytest.fixture(autouse=True)
def _clean_state():
    de.RUN_STATUS.clear()
    yield
    de.RUN_STATUS.clear()


# ── BUG-009: aborted/failed steps must not be reported as completed ──────


async def test_bug009_aborted_run_can_resume_unexecuted_steps():
    calls = []
    attempts = {"n": 0}

    async def flaky(args):
        calls.append(dict(args))
        attempts["n"] += 1
        return {"error": "boom"} if attempts["n"] == 1 else {"ok": True}

    failing = AsyncMock()
    failing.safe_execute = flaky
    tools = {
        "failing": failing,
        "nmap": _tool({"ok": True}, call_log=calls),
    }
    playbook = _pb(
        name="abort-pb",
        steps=[
            _step(id="fail", tool="failing", on_failure="abort"),
            _step(id="after", tool="nmap", depends_on=["fail"]),
        ],
    )
    with (
        patch.object(de, "_get_registry", return_value=_registry(tools)),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        first = await PlaybookEngine().run(playbook)
        state = de.RUN_STATUS["abort-pb"]
        assert state["succeeded_steps"] == []
        assert state["attempted_steps"] == [0]
        assert state["failed_steps"] == [0]
        assert state["skipped_steps"] == [1]
        # The aborted step was never executed, so it must not be reported as
        # completed either (only genuinely succeeded indices go there).
        assert json.loads(first.stdout)["completed_steps"] == []

        # Resume must re-run both the failed step and the aborted one.
        calls.clear()
        await PlaybookEngine().run(playbook, resume=True)
        assert len(calls) == 2


# ── BUG-010: checkpoint persistence must not replace the live entry ──────


async def test_bug010_persist_state_mutates_live_entry_and_saves_final_status():
    db = MagicMock()
    live_entry = {}
    de.RUN_STATUS["checkpoint-pb"] = live_entry
    with (
        patch.object(de, "_get_registry", return_value=_registry({})),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=db),
    ):
        await PlaybookEngine().run(
            _pb(
                name="checkpoint-pb",
                steps=[
                    _step(
                        id="check",
                        tool=None,
                        type="scope_check",
                        args={"target": "example.com"},
                        checkpoint=True,
                    )
                ],
            )
        )
    assert de.RUN_STATUS["checkpoint-pb"] is live_entry
    assert live_entry["status"] == "completed"
    assert live_entry["finished_at"]
    assert db.save_workflow_run.call_args.args[2] == "completed"


# ── BUG-011: MCP error envelopes are failures ────────────────────────────


def test_bug011_is_error_envelope_is_a_failure():
    engine = PlaybookEngine()
    envelope = {
        "content": [{"type": "text", "text": json.dumps({"error": "binary missing"})}],
        "isError": True,
    }
    assert engine._is_step_failure(envelope) is True
    ok_envelope = {
        "content": [{"type": "text", "text": json.dumps({"ok": True})}],
        "isError": False,
    }
    assert engine._is_step_failure(ok_envelope) is False
    assert engine._is_step_failure({"isError": True, "content": []}) is True


async def test_bug011_envelope_failure_triggers_abort():
    calls = []
    envelope = {
        "content": [{"type": "text", "text": json.dumps({"error": "bad"})}],
        "isError": True,
    }
    tools = {
        "failing": _tool(envelope, call_log=calls),
        "nmap": _tool({"ok": True}, call_log=calls),
    }
    playbook = _pb(
        name="envelope-pb",
        steps=[
            _step(id="fail", tool="failing", on_failure="abort"),
            _step(id="after", tool="nmap", depends_on=["fail"]),
        ],
    )
    with (
        patch.object(de, "_get_registry", return_value=_registry(tools)),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        await PlaybookEngine().run(playbook)
    assert len(calls) == 1
    assert de.RUN_STATUS["envelope-pb"]["failed_steps"] == [0]


# ── BUG-012: numeric / boolean equality in when conditions ───────────────


def test_bug012_when_equality_handles_numbers_and_booleans():
    engine = PlaybookEngine()
    engine._step_results["data"] = {
        "count": 0,
        "ok": True,
        "flag": "True",
        "ratio": 1.5,
        "status": "completed",
    }
    assert engine._evaluate_when("data.count == 0") is True
    assert engine._evaluate_when("data.count != 0") is False
    assert engine._evaluate_when("data.ok == true") is True
    assert engine._evaluate_when("data.ok != TRUE") is False
    assert engine._evaluate_when("data.flag == true") is True
    assert engine._evaluate_when("data.ratio == 1.5") is True
    assert engine._evaluate_when("data.status == completed") is True


# ── BUG-013: target substitution is deep and non-destructive ─────────────


async def test_bug013_target_substitution_is_deep_and_non_destructive():
    original = {
        "target": "{{target}}",
        "opts": {"host": "{{target}}"},
        "hosts": ["{{target}}", {"nested": "{{target}}"}],
        "pair": ("{{target}}",),
    }
    step = _step(id="scan", tool="nmap", args=original)
    calls = []
    tools = {"nmap": _tool({"ok": True}, call_log=calls)}
    with (
        patch.object(de, "_get_registry", return_value=_registry(tools)),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        await PlaybookEngine().run(_pb(name="p1", inputs={"target": "10.0.0.1"}, steps=[step]))
        await PlaybookEngine().run(_pb(name="p2", inputs={"target": "10.0.0.2"}, steps=[step]))

    def expected(target):
        return {
            "target": target,
            "opts": {"host": target},
            "hosts": [target, {"nested": target}],
            "pair": (target,),
        }

    assert calls == [expected("10.0.0.1"), expected("10.0.0.2")]
    assert step.args == original


# ── BUG-034: scope-gate the final args after output_mapping ──────────────


def test_bug034_extract_targets_recurses_into_nested_values():
    args = {"opts": {"host": "evil.example.com"}, "list": [{"urls": ["bad.example.net"]}]}
    assert extract_targets(args) == ["evil.example.com", "bad.example.net"]


async def test_bug034_mapped_target_is_scope_checked_before_tool_runs():
    calls = []

    def gate(args, *, step_id, tool_name=None, scope=None):
        if "evil.example.net" in json.dumps(args, default=str):
            raise ScopeViolationError("mapped target out of scope")

    tools = {
        "discover": _tool({"host": "evil.example.net"}),
        "nmap": _tool({"ok": True}, call_log=calls),
    }
    playbook = _pb(
        name="mapped-pb",
        steps=[
            _step(id="discover", tool="discover", args={}),
            _step(
                id="scan",
                tool="nmap",
                args={"ports": "22"},
                output_mapping={"target": "discover.host"},
                depends_on=["discover"],
                on_failure="abort",
            ),
        ],
    )
    with (
        patch.object(de, "_get_registry", return_value=_registry(tools)),
        patch.object(de, "gate_step_args", new=gate),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        engine = PlaybookEngine()
        engine._step_results["discover"] = {"host": "evil.example.net"}
        await engine.run(playbook)
    assert len(calls) == 0
    assert de.RUN_STATUS["mapped-pb"]["failed_steps"] == [1]


# ── BUG-033: skip_dependents only skips transitive dependents ─────────────


async def test_bug033_skip_dependents_keeps_independent_branches():
    calls = []
    tools = {"failing": _tool({"error": "x"}, call_log=calls), "nmap": _tool({"ok": True})}

    def record(args):
        calls.append(dict(args))
        return {"ok": True}

    independent = _tool({"ok": True})
    independent.safe_execute = AsyncMock(side_effect=record)
    tools["independent"] = independent

    playbook = _pb(
        name="skip-pb",
        steps=[
            _step(id="fail", tool="failing", on_failure="skip_dependents"),
            _step(id="dep", tool="nmap", depends_on=["fail"], args={"which": "dep"}),
            _step(id="other", tool="independent", args={"which": "other"}),
            _step(id="other_dep", tool="independent", depends_on=["other"], args={"which": "od"}),
        ],
    )
    with (
        patch.object(de, "_get_registry", return_value=_registry(tools)),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        await PlaybookEngine().run(playbook)
    assert [c.get("which") for c in calls if "which" in c] == ["other", "od"]
    assert de.RUN_STATUS["skip-pb"]["skipped_steps"] == [1]


# ── BUG-035: when expressions are reference-validated ────────────────────


def _validated(steps):
    return validate_playbook(
        _pb(name="when-pb", steps=steps),
        tool_catalog=["nmap"],
    )


def test_bug035_when_unknown_reference_is_an_error():
    findings = _validated(
        [
            _step(id="first"),
            _step(id="second", when="nosuch.count > 0", depends_on=["first"]),
        ]
    )
    assert any(
        f["severity"] == "error" and f["field"] == "when" and "unknown step" in f["message"]
        for f in findings
    )


def test_bug035_when_valid_reference_passes():
    findings = _validated(
        [
            _step(id="first"),
            _step(id="second", when="first.count > 0", depends_on=["first"]),
        ]
    )
    assert is_valid(findings)


# ── BUG-036: bare dotted foreach references ──────────────────────────────


def test_bug036_bare_foreach_step_reference_is_valid():
    findings = validate_playbook(
        _pb(
            name="foreach-pb",
            steps=[
                _step(id="discover", tool="nmap"),
                _step(
                    id="scan",
                    tool="nmap",
                    foreach="discover.hosts",
                    depends_on=["discover"],
                ),
            ],
        ),
        tool_catalog=["nmap"],
    )
    assert is_valid(findings)


# ── BUG-037: DSL round-trip for max_iterations / array / object ───────────


ROUNDTRIP_DSL = """id: custom.roundtrip
name: "Roundtrip"
inputs {
  hosts: array: [string]
  opts: object: {}
}
execution {
  max_parallel: 2
  max_iterations: 50
}
steps {
  scan: tool nmap inputs {
    target: "{{inputs.hosts}}"
  }
}
"""


def test_bug037_dsl_roundtrip_preserves_execution_and_types():
    defn1 = compile_dsl(ROUNDTRIP_DSL)
    assert defn1.execution["max_iterations"] == 50
    assert defn1.inputs["hosts"]["element_type"] == "string"
    assert defn1.inputs["opts"]["type"] == "object"

    text = deserialize(defn1)
    defn2 = compile_dsl(text)
    assert defn2.execution == defn1.execution
    assert defn2.inputs == defn1.inputs


# ── BUG-062: standalone workflow import ──────────────────────────────────


def test_bug062_workflow_imports_on_fresh_interpreter():
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", "import kmcp.workflow; print('ok')"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
