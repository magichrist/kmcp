"""Regression tests for R-05: foreach binds the iterated item into each clone.

Before the fix, `_run_foreach` injected `_foreach_item` only as a raw key but
never substituted ``{{item}}`` / ``{{_foreach_item}}`` / ``{item}`` into the
cloned step's args, inputs or command — so tool steps received the literal
placeholder and shell steps failed with "unresolved placeholder(s) ['item']".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kmcp.workflow import dag_executor as de
from kmcp.workflow.dag_executor import PlaybookEngine
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


def _step(
    id="s",
    tool=None,
    module=None,
    type="tool",
    depends_on=None,
    when=None,
    foreach=None,
    output_mapping=None,
    retry=0,
    on_failure="abort",
    checkpoint=False,
    budget=None,
    args=None,
    command=None,
    inputs=None,
    parallel=False,
):
    return StepDefinition(
        id=id,
        tool=tool,
        module=module,
        type=type,
        depends_on=list(depends_on or []),
        when=when,
        parallel=parallel,
        foreach=foreach,
        output_mapping=dict(output_mapping or {}),
        retry=retry,
        on_failure=on_failure,
        checkpoint=checkpoint,
        budget=dict(budget or {}),
        args=dict(args or {}),
        command=command,
        inputs=dict(inputs or {}),
    )


def _tool(result=None, *, call_log=None):
    async def _safe_execute(args):
        if call_log is not None:
            call_log.append(args)
        return result if result is not None else {"ok": True}

    tool = AsyncMock()
    tool.safe_execute = _safe_execute
    return tool


def _clear_run_status():
    de.RUN_STATUS.clear()


@pytest.mark.asyncio
async def test_foreach_shell_command_binds_item_per_iteration():
    """Shell step: command contains {{item}} → each iteration runs with the
    concrete value substituted."""
    _clear_run_status()
    call_log = []
    tools = {"dnsx": _tool(None, call_log=call_log)}
    engine = PlaybookEngine()
    engine._playbook_inputs = {}
    # Simulate an upstream step that discovered the ports.
    engine._step_results["discover"] = {"ports": [80, 443]}

    with patch(
        "kmcp.workflow.dag_executor._get_registry",
        return_value=SimpleNamespace(get=lambda name: tools.get(name)),
    ), patch(
        "kmcp.execution.engine.execute",
        new=AsyncMock(
            return_value=SimpleNamespace(
                stdout="", stderr="", exit_code=0, success=True,
                timed_out=False, duration=0.0, started_at="t",
            )
        ),
    ):
        res = await engine._run_foreach(
            _step(
                id="iter",
                type="shell",
                command="echo ITEM_{item}",
                inputs={"item": "{{item}}"},
                foreach="discover.ports",
            ),
            [_step(id="iter", type="shell", command="echo ITEM_{item}", inputs={"item": "{{item}}"}, foreach="discover.ports")],
            {},
        )

    assert res["count"] == 2
    cmds = [r.get("command") for r in res["foreach_results"] if r.get("command")]
    assert any("ITEM_80" in c for c in cmds), cmds
    assert any("ITEM_443" in c for c in cmds), cmds


@pytest.mark.asyncio
async def test_foreach_tool_args_bind_item_per_iteration():
    """Tool step: args.target contains {{item}} → each call receives the
    concrete value, not a literal placeholder."""
    _clear_run_status()
    call_log = []
    tools = {"dnsx": _tool(None, call_log=call_log)}
    engine = PlaybookEngine()
    engine._step_results["discover"] = {"ports": [80, 443]}

    with patch(
        "kmcp.workflow.dag_executor._get_registry",
        return_value=SimpleNamespace(get=lambda name: tools.get(name)),
    ):
        res = await engine._run_foreach(
            _step(
                id="iter",
                tool="dnsx",
                args={"target": "{{item}}"},
                foreach="discover.ports",
            ),
            [_step(id="iter", tool="dnsx", args={"target": "{{item}}"}, foreach="discover.ports")],
            {},
        )

    assert res["count"] == 2
    targets = [c.get("target") for c in call_log]
    assert targets == [80, 443] or targets == ["80", "443"], targets


@pytest.mark.asyncio
async def test_foreach_placeholder_form_variants():
    """``{{_foreach_item}}`` and ``{item}`` also resolve (plain value ref too)."""
    _clear_run_status()
    call_log = []
    tools = {"dnsx": _tool(None, call_log=call_log)}
    engine = PlaybookEngine()
    engine._step_results["discover"] = {"items": ["alpha", "beta"]}

    with patch(
        "kmcp.workflow.dag_executor._get_registry",
        return_value=SimpleNamespace(get=lambda name: tools.get(name)),
    ):
        res = await engine._run_foreach(
            _step(
                id="iter",
                tool="dnsx",
                args={"target": "{{_foreach_item}}", "plain": "_foreach_item"},
                foreach="discover.items",
            ),
            [_step(id="iter", tool="dnsx", args={"target": "{{_foreach_item}}", "plain": "_foreach_item"}, foreach="discover.items")],
            {},
        )

    assert res["count"] == 2
    assert call_log[0]["target"] in ("alpha", "beta")
    assert call_log[0]["plain"] == call_log[0]["target"]
