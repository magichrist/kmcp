"""R-02 regression: ``job_control`` must assess jobs stored by assessment id.

Assessment rows are keyed by assessment id while the job id lives in the
``job:<id>`` tag / JSON content. These tests use a real SQLite vault rooted
in a temp directory and hermetic in-memory job registries — no network and
no full-suite side effects.
"""

from __future__ import annotations

import json

import pytest

from kmcp.tools import agent_interface as ai
from kmcp.tools.agent_interface import JobControlTool
from kmcp.tools.parallel_execute import JOBS as PARALLEL_JOBS
from kmcp.utils import vault as vault_mod


def _payload(res: dict) -> dict:
    outer = json.loads(res["content"][0]["text"])
    return json.loads(outer["stdout"])


@pytest.fixture()
def assessment_vault(tmp_path, monkeypatch):
    """Temp vault containing an assessment row keyed by assessment id only."""
    monkeypatch.setattr(vault_mod, "VAULT_ROOT", tmp_path / "vault")
    vault_mod.vault_write(
        category="assessments",
        name="assessment_x",
        content={
            "assessment_id": "assessment_x",
            "job_id": "job_y",
            "status": "completed",
            "summary": {"ok": True},
        },
        tags="assessment:assessment_x,job:job_y",
    )
    monkeypatch.setattr(ai.engine, "get_active_jobs", list)
    yield


@pytest.fixture()
def clean_job_registries():
    ai.BATCH_JOBS.clear()
    PARALLEL_JOBS.clear()
    yield
    ai.BATCH_JOBS.clear()
    PARALLEL_JOBS.clear()


async def test_r02_job_control_resolves_assessment_job_by_tag(assessment_vault):
    """The reported repro: name is the assessment id, job id is in tags."""
    rows = vault_mod.vault_query(category="assessments", search="job_y")
    assert [e["name"] for e in rows["entries"]] == ["assessment_x"]
    assert "job:job_y" in rows["entries"][0]["tags"]

    res = await JobControlTool().execute({"job_id": "job_y", "action": "status"})
    payload = _payload(res)

    assert payload["status"] == "completed"
    assert payload["job_id"] == "job_y"
    assert payload["assessment_id"] == "assessment_x"
    assert payload["result"]["summary"] == {"ok": True}


async def test_r02_unknown_job_still_not_found(assessment_vault):
    res = await JobControlTool().execute({"job_id": "job_does_not_exist", "action": "status"})
    assert _payload(res)["status"] == "not_found"


async def test_r02_job_id_in_content_only_is_resolved(tmp_path, monkeypatch):
    """vault_query omits content, so the row must be read to match content."""
    monkeypatch.setattr(vault_mod, "VAULT_ROOT", tmp_path / "vault")
    vault_mod.vault_write(
        category="assessments",
        name="assessment_content_only",
        content={
            "assessment_id": "assessment_content_only",
            "job_id": "job_z",
            "status": "running",
        },
        tags="assessment:assessment_content_only",
    )
    monkeypatch.setattr(ai.engine, "get_active_jobs", list)

    res = await JobControlTool().execute({"job_id": "job_z", "action": "status"})
    payload = _payload(res)

    assert payload["status"] == "running"
    assert payload["assessment_id"] == "assessment_content_only"


def test_r02_matcher_handles_list_tags_and_dict_content():
    assert ai._entry_matches_job_id(
        {"name": "assessment_x", "tags": ["assessment:assessment_x", "job:job_y"]},
        "job_y",
    )
    assert ai._entry_matches_job_id(
        {"name": "assessment_x", "content": {"job_id": "job_y"}}, "job_y"
    )
    assert ai._entry_matches_job_id(
        {"name": "assessment_x", "content": '{"job_id": "job_y"}'}, "job_y"
    )
    assert ai._entry_matches_job_id({"name": "job_y"}, "job_y")
    assert not ai._entry_matches_job_id({"name": "assessment_x", "tags": "job:other"}, "job_y")


async def test_r02_parallel_job_still_delegates(assessment_vault, clean_job_registries):
    PARALLEL_JOBS["par_r02"] = {"status": "completed", "total": 1, "done": 1}

    res = await JobControlTool().execute({"job_id": "par_r02", "action": "status"})
    payload = _payload(res)

    assert payload["tool"] == "parallel_execute"
    assert payload["status"] == "completed"
    assert payload["result"]["done"] == 1


async def test_r02_batch_job_still_delegates(assessment_vault, clean_job_registries):
    ai.BATCH_JOBS["batch_r02"] = {
        "status": "completed",
        "batch_id": "batch_r02",
        "operations": [],
    }

    res = await JobControlTool().execute({"job_id": "batch_r02", "action": "status"})
    payload = _payload(res)

    assert payload["tool"] == "batch_run"
    assert payload["status"] == "completed"
    assert payload["result"]["batch_id"] == "batch_r02"

    cancelled = await JobControlTool().execute({"job_id": "batch_r02", "action": "cancel"})
    assert _payload(cancelled)["status"] == "cancelled"
    assert ai.BATCH_JOBS["batch_r02"]["status"] == "cancelled"
