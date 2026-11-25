"""Focused regressions for handoff findings F-03, F-09 and F-12.

All tests use a temporary SQLite DB and monkeypatched registries; nothing is
written to the host environment. Run with::

    .venv/bin/python -m pytest tests/unit/test_bugfix_handoff_b.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kmcp.db.manager import DBManager
from kmcp.tools.playbook_blackbox import (
    JOB_REGISTRY,
    PlaybookCatalogTool,
    PlaybookRunTool,
)
from kmcp.tools.template_tools import TemplateListTool, TemplateRunTool


@pytest.fixture
def db(tmp_path: Path):
    manager = DBManager(tmp_path / "bugfix_handoff_b.sqlite")
    manager.ensure_schema()
    yield manager
    manager.conn.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, db):
    """Route all DB access to the temp DB and clear in-memory job state."""
    JOB_REGISTRY.clear()
    monkeypatch.setattr("kmcp.db.manager.get_db", lambda: db)
    monkeypatch.setattr("kmcp.tools.playbook_blackbox.get_current_profile", lambda: None)
    yield
    JOB_REGISTRY.clear()


def _tool_payload(result: dict) -> dict:
    return json.loads(json.loads(result["content"][0]["text"])["stdout"])


def _error_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _seed_published(db, playbook_id: str, definition: dict, status: str = "published") -> int:
    """Create a playbook row and a published version carrying ``definition``."""
    db.create_playbook(playbook_id, f"Name {playbook_id}", status="draft")
    version_id = db.publish_playbook_version(
        playbook_id,
        "1.0.0",
        definition=definition,
        published_by="test",
    )
    if status != "published":
        db.update_playbook(playbook_id, status=status)
    return version_id


# ── F-03: playbook_catalog with no agent profile ─────────────────────────────


def _pb_definition(description: str, inputs: dict | None = None) -> dict:
    return {
        "description": description,
        "inputs": inputs or {"target": {"type": "string"}, "depth": {"type": "string"}},
        "outputs": {"findings": {}, "assets": {}},
        "execution": {"timeout": 120},
    }


async def test_catalog_no_profile_merges_published_db_and_builtin_compact(db, monkeypatch):
    definition = _pb_definition("from studio")
    _seed_published(db, "studio-pb", definition)
    _seed_published(db, "draft-pb", _pb_definition("draft"), status="draft")
    builtin = [
        {
            "id": "builtin-pb",
            "name": "Builtin",
            "description": "builtin desc",
            "inputs": {},
            "outputs": {},
            "estimated_duration": 300,
        }
    ]
    monkeypatch.setattr("kmcp.playbooks.registry.list_playbooks", lambda: builtin)

    result = await PlaybookCatalogTool().execute({"detail": "compact"})
    assert result["isError"] is False
    payload = _tool_payload(result)
    entries = {p["id"]: p for p in payload["playbooks"]}

    assert set(entries) == {"builtin-pb", "studio-pb"}  # drafts stay hidden
    assert payload["count"] == 2
    studio = entries["studio-pb"]
    assert studio["source"] == "studio"
    assert studio["description"] == "from studio"
    assert studio["inputs"] == ["depth", "target"]  # compact exposes input names
    assert studio["outputs"] == ["assets", "findings"]
    assert studio["estimated_duration"] == 120
    # Builtin shape is unchanged.
    assert entries["builtin-pb"] == {
        "id": "builtin-pb",
        "name": "Builtin",
        "description": "builtin desc",
        "estimated_duration": 300,
    }


async def test_catalog_no_profile_full_includes_studio_definition(db, monkeypatch):
    definition = _pb_definition("full studio")
    _seed_published(db, "studio-pb", definition)
    builtin = [{"id": "builtin-pb", "name": "Builtin", "description": "d", "inputs": {}, "outputs": {}}]
    monkeypatch.setattr("kmcp.playbooks.registry.list_playbooks", lambda: builtin)

    result = await PlaybookCatalogTool().execute({"detail": "full"})
    entries = {p["id"]: p for p in _tool_payload(result)["playbooks"]}
    assert entries["studio-pb"]["definition"] == definition
    assert "inputs" not in entries["studio-pb"]  # full means definition, not compact keys
    assert entries["builtin-pb"] == builtin[0]  # builtin entries preserved verbatim


async def test_catalog_no_profile_db_failure_still_returns_builtins(db, monkeypatch):
    builtin = [{"id": "builtin-pb", "name": "B", "description": "d", "estimated_duration": 60}]
    monkeypatch.setattr("kmcp.playbooks.registry.list_playbooks", lambda: builtin)
    monkeypatch.setattr(db, "list_playbooks", MagicMock(side_effect=RuntimeError("db down")))

    result = await PlaybookCatalogTool().execute({"detail": "compact"})
    payload = _tool_payload(result)
    assert payload["count"] == 1
    assert payload["playbooks"][0]["id"] == "builtin-pb"


async def test_catalog_with_profile_keeps_assignment_filtering(db, monkeypatch):
    """A profile still sees only its assigned published playbooks."""
    _seed_published(db, "assigned-pb", _pb_definition("assigned"))
    _seed_published(db, "other-pb", _pb_definition("other"))
    monkeypatch.setattr("kmcp.tools.playbook_blackbox.get_current_profile", lambda: "agent-1")
    monkeypatch.setattr(
        "kmcp.tools.playbook_blackbox.agent_context.assigned_playbooks",
        lambda profile: [db.get_playbook("assigned-pb")],
    )
    monkeypatch.setattr(
        "kmcp.playbooks.registry.list_playbooks",
        MagicMock(side_effect=AssertionError("builtin path must not run with a profile")),
    )

    result = await PlaybookCatalogTool().execute({"detail": "compact"})
    payload = _tool_payload(result)
    assert payload["profile_id"] == "agent-1"
    assert [p["id"] for p in payload["playbooks"]] == ["assigned-pb"]


# ── F-03: template_list with no agent profile ───────────────────────────────


def _template_definition(description: str = "studio template") -> dict:
    return {
        "description": description,
        "parameters": {
            "target": {"type": "string", "required": True, "description": "target host"},
            "depth": {"type": "string", "required": False, "default": "normal"},
        },
        "steps": [],
    }


async def test_template_list_no_profile_merges_published_db_templates(db, monkeypatch):
    template_def = _template_definition()
    _seed_published(db, "studio-template", template_def)
    _seed_published(db, "plain-pb", {"description": "no params", "steps": []})
    builtin = [
        {
            "id": "builtin-template",
            "name": "Builtin template",
            "description": "builtin",
            "parameters": {"token": {"required": False}},
        }
    ]
    monkeypatch.setattr("kmcp.playbooks.registry.list_templates", lambda: builtin)

    result = await TemplateListTool().execute({"detail": "compact"})
    assert result["isError"] is False
    payload = _tool_payload(result)
    entries = {t["id"]: t for t in payload["templates"]}
    assert set(entries) == {"builtin-template", "studio-template"}
    assert payload["count"] == 2
    studio = entries["studio-template"]
    assert studio["description"] == "studio template"
    assert studio["required_params"] == ["target"]
    assert "definition" not in studio  # compact stays compact
    assert entries["builtin-template"] == {
        "id": "builtin-template",
        "name": "Builtin template",
        "description": "builtin",
        "required_params": [],
    }


async def test_template_list_full_includes_studio_definition(db, monkeypatch):
    template_def = _template_definition()
    _seed_published(db, "studio-template", template_def)
    monkeypatch.setattr("kmcp.playbooks.registry.list_templates", list)

    result = await TemplateListTool().execute({"detail": "full"})
    payload = _tool_payload(result)
    assert payload["count"] == 1
    entry = payload["templates"][0]
    assert entry["definition"] == template_def
    assert entry["parameters"]["target"]["required"] is True
    assert entry["parameters"]["depth"]["default"] == "normal"


# ── F-12: template_run structured errors ────────────────────────────────────


async def test_template_run_missing_id_returns_structured_error():
    result = await TemplateRunTool().execute({})
    assert result["isError"] is True
    payload = _error_payload(result)
    assert payload["success"] is False
    assert payload["error_type"] == "ValueError"
    assert "template_id is required" in payload["error"]


async def test_template_run_nonexistent_id_returns_structured_error(monkeypatch):
    from kmcp.playbooks.registry import TemplateNotFound

    monkeypatch.setattr(
        "kmcp.playbooks.registry.render_template",
        MagicMock(side_effect=TemplateNotFound("Template not found: no-such-template")),
    )
    result = await TemplateRunTool().execute({"template_id": "no-such-template"})
    assert result["isError"] is True
    payload = _error_payload(result)
    assert payload["success"] is False
    assert payload["error_type"] == "TemplateNotFound"
    assert "no-such-template" in payload["error_message"]


async def test_template_run_engine_failure_returns_structured_error(monkeypatch):
    monkeypatch.setattr(
        "kmcp.playbooks.registry.render_template", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        "kmcp.workflow.dag_executor.run_playbook_async",
        AsyncMock(side_effect=RuntimeError("engine boom")),
    )
    result = await TemplateRunTool().execute({"template_id": "t", "target": "host.test"})
    assert result["isError"] is True
    payload = _error_payload(result)
    assert payload["success"] is False
    assert payload["error_type"] == "RuntimeError"
    assert "engine boom" in payload["error"]
    failed = [s for s in JOB_REGISTRY.values() if s["status"] == "failed"]
    assert len(failed) == 1
    assert "engine boom" in failed[0]["error"]


async def test_template_run_success_path_unchanged(monkeypatch):
    monkeypatch.setattr(
        "kmcp.playbooks.registry.render_template", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        "kmcp.workflow.dag_executor.run_playbook_async",
        AsyncMock(return_value=SimpleNamespace(status="completed", stdout="{}")),
    )
    result = await TemplateRunTool().execute({"template_id": "t", "target": "host.test"})
    assert result["isError"] is False
    payload = _tool_payload(result)
    assert payload["status"] == "completed"
    assert payload["template"] == "t"
    assert result["template_run"] == payload


# ── F-09: playbook_run deposits an assessments vault row ─────────────────────


def _assessment() -> dict:
    return {
        "assessment_id": "assessment_fixed",
        "summary": {"assets": 1, "findings": 1},
        "findings": [{"id": "f1", "title": "Open port", "severity": "high"}],
        "next_actions": ["Close the port"],
        "execution": {"status": "completed"},
    }


async def test_playbook_run_legacy_completion_writes_assessment_row(db, monkeypatch):
    monkeypatch.setattr(
        "kmcp.playbooks.registry.get_playbook",
        lambda pb_id: {"execution": {"timeout": 10}},
    )
    engine = MagicMock()
    engine.execute = AsyncMock(return_value=_assessment())
    monkeypatch.setattr("kmcp.playbooks.engine.PlaybookEngine", MagicMock(return_value=engine))
    writes = []
    monkeypatch.setattr(
        "kmcp.utils.vault.vault_write", lambda **kwargs: writes.append(kwargs)
    )

    result = await PlaybookRunTool().execute({"playbook": "legacy-pb", "target": "host.test"})
    assert result["isError"] is False
    assert len(writes) == 1
    write = writes[0]
    assert write["category"] == "assessments"
    assert write["name"] == "assessment_fixed"
    assert write["target"] == "host.test"
    assert write["tool"] == "playbook_run"
    content = write["content"]
    assert content["assessment_id"] == "assessment_fixed"
    assert content["job_id"] == _tool_payload(result)["job_id"]
    assert content["status"] == "completed"
    assert content["summary"] == {"assets": 1, "findings": 1}
    assert content["next_actions"] == ["Close the port"]
    assert content["findings"][0]["title"] == "Open port"
    for key in ("id", "severity", "confidence", "validation_status", "asset_id", "description"):
        assert key in content["findings"][0]


async def test_playbook_run_template_completion_writes_assessment_row(monkeypatch):
    monkeypatch.setattr(
        "kmcp.playbooks.registry.render_template", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        "kmcp.workflow.dag_executor.run_playbook_async",
        AsyncMock(
            return_value=SimpleNamespace(
                status="completed",
                stdout=json.dumps(
                    {
                        "findings": [{"id": "tf1", "severity": "low"}],
                        "next_actions": ["review"],
                    }
                ),
            )
        ),
    )
    writes = []
    monkeypatch.setattr(
        "kmcp.utils.vault.vault_write", lambda **kwargs: writes.append(kwargs)
    )

    result = await PlaybookRunTool().execute({"template": "tpl", "target": "host.test"})
    assert result["isError"] is False
    assert len(writes) == 1
    assert writes[0]["category"] == "assessments"
    assert writes[0]["content"]["assessment_id"] == _tool_payload(result)["assessment_id"]
    assert writes[0]["content"]["status"] == "completed"


async def test_playbook_run_studio_completion_writes_assessment_row(db, monkeypatch):
    _seed_published(db, "studio-pb", _pb_definition("studio run"))
    monkeypatch.setattr(
        "kmcp.tools.playbook_blackbox._studio_run_definition",
        lambda playbook, inputs: (
            SimpleNamespace(inputs={}),
            {"version_id": 1, "version": "1.0.0"},
        ),
    )
    monkeypatch.setattr(
        "kmcp.workflow.dag_executor.run_playbook_async",
        AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                stdout=json.dumps({"findings": [{"id": "sf1"}], "next_actions": ["next"]}),
            )
        ),
    )
    writes = []
    monkeypatch.setattr(
        "kmcp.utils.vault.vault_write", lambda **kwargs: writes.append(kwargs)
    )

    result = await PlaybookRunTool().execute({"playbook": "studio-pb", "target": "host.test"})
    assert result["isError"] is False
    assert len(writes) == 1
    assert writes[0]["category"] == "assessments"
    assert writes[0]["name"] == _tool_payload(result)["assessment_id"]
    assert writes[0]["content"]["findings"][0]["id"] == "sf1"


async def test_vault_failure_does_not_break_playbook_run(monkeypatch):
    monkeypatch.setattr(
        "kmcp.playbooks.registry.get_playbook",
        lambda pb_id: {"execution": {"timeout": 10}},
    )
    engine = MagicMock()
    engine.execute = AsyncMock(return_value=_assessment())
    monkeypatch.setattr("kmcp.playbooks.engine.PlaybookEngine", MagicMock(return_value=engine))
    monkeypatch.setattr(
        "kmcp.utils.vault.vault_write", MagicMock(side_effect=RuntimeError("vault down"))
    )

    result = await PlaybookRunTool().execute({"playbook": "legacy-pb", "target": "host.test"})
    assert result["isError"] is False
    assert _tool_payload(result)["assessment_id"] == "assessment_fixed"
