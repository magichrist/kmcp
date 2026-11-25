"""Focused regressions for builder-handoff defects F-06, F-04 and F-12.

The tests exercise the real workflow engine / validator with mocked tools, DB
and execution backend — no network calls and no full-suite side effects.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kmcp.models import ExecutionResult
from kmcp.workflow import dag_executor as de
from kmcp.workflow.dag_executor import PlaybookEngine
from kmcp.workflow.validator import is_valid, validate_playbook
from kmcp.workflow.yaml_loader import (
    PlaybookDefinition,
    StepDefinition,
    load_playbook,
)


def _pb(name="pb", steps=None, inputs=None, execution=None):
    return PlaybookDefinition(
        name=name,
        version="1.0",
        inputs=inputs or {},
        execution=execution or {"max_parallel": 4, "timeout": 900},
        steps=steps or [],
        raw={},
    )


def _step(id="s", tool=None, args=None, **kwargs):
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


def _fake_result(stdout: str, exit_code: int = 0) -> ExecutionResult:
    return ExecutionResult(
        tool="shell_command",
        command="x",
        stdout=stdout,
        stderr="",
        exit_code=exit_code,
        success=exit_code == 0,
        timed_out=False,
        duration=0.01,
        start_time="",
        end_time="",
    )


@pytest.fixture(autouse=True)
def _clean_state():
    de.RUN_STATUS.clear()
    yield
    de.RUN_STATUS.clear()


# ── F-06: foreach must preserve the whole step definition ────────────────


async def test_f06_foreach_shell_step_runs_command_and_filter_per_item(monkeypatch):
    """A shell step with foreach keeps command/inputs/filter across clones."""
    commands: list[str] = []

    async def fake_execute(cmd: Any, **kwargs: Any) -> ExecutionResult:
        commands.append(str(cmd))
        return _fake_result("ITEM_alpha up")

    monkeypatch.setattr(de, "gate_step_args", lambda *a, **kw: None)
    monkeypatch.setattr("kmcp.execution.engine.execute", fake_execute)

    step = StepDefinition(
        id="echo",
        type="shell",
        command="echo ITEM_{item}",
        inputs={"item": "alpha"},  # literal value; replaces {item} in command
        filter={"type": "grep", "pattern": r"ITEM_(?P<tag>[a-z]+)", "first_only": True},
        foreach="discover.items",
        depends_on=["discover"],
    )
    engine = PlaybookEngine()
    engine._playbook_inputs = {}
    engine._subst_target = None
    engine._step_results["discover"] = {"items": ["host1", "host2"]}

    res = await engine._run_foreach(step, [step], {})

    assert res["count"] == 2
    # The defect dropped `command`, so execute() was never called at all.
    assert commands == ["echo ITEM_alpha", "echo ITEM_alpha"]
    for item in res["foreach_results"]:
        assert item.get("error") is None
        assert item["command"] == "echo ITEM_alpha"
        # `filter` survived the clone too.
        assert item["tag"] == "alpha"
        assert item["match_count"] == 1


async def test_f06_foreach_clone_preserves_all_fields_and_clears_foreach(monkeypatch):
    """The per-iteration clone is a full copy, with foreach cleared."""
    seen: list[StepDefinition] = []

    async def capture(iter_step, state):
        seen.append(iter_step)
        return {"step": iter_step.id, "ok": True}

    original = StepDefinition(
        id="probe",
        type="shell",
        command="echo {item}",
        inputs={"item": "x"},
        filter={"type": "grep", "pattern": "x"},
        args={"timeout": 5, "flag": "keep"},
        budget={"timeout": 30},
        on_failure="continue",
        retry=2,
        output_mapping={"out": "probe.x"},
        checkpoint=True,
        foreach="discover.items",
        depends_on=["discover"],
    )
    engine = PlaybookEngine()
    engine._step_results["discover"] = {"items": ["a"]}
    monkeypatch.setattr(de, "gate_step_args", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_execute_single_step", capture)

    await engine._run_foreach(original, [original], {})

    assert len(seen) == 1
    clone = seen[0]
    assert clone.id == "probe_foreach"
    assert clone.command == original.command
    assert clone.inputs == original.inputs
    assert clone.filter == original.filter
    assert clone.budget == original.budget
    assert clone.on_failure == original.on_failure
    assert clone.retry == original.retry
    assert clone.output_mapping == original.output_mapping
    assert clone.checkpoint == original.checkpoint
    assert clone.depends_on == original.depends_on
    # The item is injected into args, and recursion is disabled.
    assert clone.args["_foreach_item"] == "a"
    assert clone.args["flag"] == "keep"
    assert clone.foreach is None
    # Original definition must be untouched.
    assert original.foreach == "discover.items"


# ── F-04: validation of arg placeholders / misplaced input specs ────────


def _definition(inputs: dict, args: dict) -> dict:
    return {
        "name": "f04",
        "version": "1.0",
        "inputs": inputs,
        "execution": {"max_parallel": 2, "timeout": 60},
        "steps": [{"id": "scan", "tool": "nmap", "args": args}],
    }


def _findings(definition: dict) -> list[dict]:
    return validate_playbook(load_playbook(definition), tool_catalog=["nmap"])


def test_f04_declared_target_reference_is_accepted():
    findings = _findings(
        _definition(
            {"target": {"type": "string", "required": True}},
            {"target": "{{target}}"},
        )
    )
    assert is_valid(findings)


def test_f04_declared_input_reference_is_accepted():
    findings = _findings(
        _definition(
            {"host": {"type": "string", "required": True}},
            {"opts": {"target": "{{inputs.host}}", "ports": "22"}},
        )
    )
    assert is_valid(findings)


def test_f04_undeclared_target_placeholder_in_args_is_error():
    findings = _findings(_definition({}, {"target": "{{target}}"}))
    assert any(
        f["severity"] == "error" and "no 'target' input" in f["message"] for f in findings
    )
    assert not is_valid(findings)


def test_f04_undeclared_input_placeholder_in_args_is_error():
    findings = _findings(_definition({}, {"target": "{{inputs.nope}}"}))
    assert any(
        f["severity"] == "error" and "undeclared input" in f["message"] for f in findings
    )
    assert not is_valid(findings)


def test_f04_input_spec_fragment_in_args_is_error():
    findings = _findings(
        _definition({}, {"target": {"type": "string", "description": "target to scan"}})
    )
    assert any(
        f["severity"] == "error" and "input declaration fragment" in f["message"]
        for f in findings
    )
    assert not is_valid(findings)


def test_f04_nested_input_spec_fragment_in_args_is_error():
    findings = _findings(
        _definition(
            {"host": {"type": "string"}},
            {"opts": [{"target": {"type": "string", "description": "host"}}]},
        )
    )
    assert any(
        f["severity"] == "error" and "input declaration fragment" in f["message"]
        for f in findings
    )


def test_f04_placeholder_with_input_spec_body_is_error():
    findings = _findings(
        _definition({}, {"target": "{{type: string, description: scan target}}"})
    )
    assert any(
        f["severity"] == "error" and "unsupported variable reference" in f["message"]
        for f in findings
    )


def test_f04_ordinary_nested_dict_args_stay_valid():
    findings = _findings(
        _definition(
            {"host": {"type": "string"}},
            {"opts": {"target": "{{inputs.host}}", "type": "syn", "ports": "22"}},
        )
    )
    assert is_valid(findings)


@pytest.mark.asyncio
async def test_f04_validate_tool_reports_bad_args_as_invalid():
    from kmcp.studio import playbook_service
    from kmcp.tools.playbook_authoring import PlaybookValidateTool

    definition = _definition({}, {"target": {"type": "string", "description": "target"}})

    def real_validate(db, playbook_id, defn, *, for_publish=False):
        findings = validate_playbook(load_playbook(defn), tool_catalog=["nmap"])
        return {"valid": is_valid(findings), "findings": findings}

    with (
        patch("kmcp.tools.playbook_authoring.db_manager.get_db", return_value=MagicMock()),
        patch.object(playbook_service, "validate_definition", side_effect=real_validate),
    ):
        response = await PlaybookValidateTool().execute(
            {"playbook_id": "f04", "definition": definition, "for_publish": True}
        )

    body = json.loads(response["content"][0]["text"])
    payload = json.loads(body["stdout"])
    assert payload["valid"] is False
    assert "input declaration fragment" in payload["readable"]


# ── F-12: false `when` steps belong only in skipped_steps ────────────────


async def test_f12_false_when_step_counted_only_as_skipped():
    calls: list[dict] = []
    probe = _tool({"count": 0})
    gated = _tool({"ok": True}, call_log=calls)
    playbook = _pb(
        name="when-skip-pb",
        steps=[
            _step(id="probe", tool="probe"),
            _step(id="gated", tool="gated", when="probe.count > 0", depends_on=["probe"]),
        ],
    )
    with (
        patch.object(de, "_get_registry", return_value=_registry({"probe": probe, "gated": gated})),
        patch.object(de, "gate_step_args"),
        patch("kmcp.db.manager.get_db", return_value=None),
    ):
        result = await PlaybookEngine().run(playbook)

    # The conditionally skipped step must never be executed.
    assert calls == []

    state = de.RUN_STATUS["when-skip-pb"]
    assert state["skipped_steps"] == [1]
    assert state["succeeded_steps"] == [0]
    assert state["completed_steps"] == [0]
    assert state["attempted_steps"] == [0]
    assert state["failed_steps"] == []

    payload = json.loads(result.stdout)
    assert payload["skipped_steps"] == [1]
    assert payload["succeeded_steps"] == [0]
    assert payload["completed_steps"] == [0]
    assert payload["attempted_steps"] == [0]
    assert payload["failed_steps"] == []
