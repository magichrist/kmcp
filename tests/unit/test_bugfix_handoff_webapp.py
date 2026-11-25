"""Regression test: built-in webapp playbook gives nuclei/commix enough time (F-05)."""

from kmcp.tools.playbook_webapp import PlaybookWebappTool


def test_webapp_playbook_sets_longer_timeouts_for_slow_steps():
    definition = PlaybookWebappTool()._playbook_definition(
        {"target": "http://127.0.0.1:8000", "timeout": 2400}
    )
    steps = {s["id"]: s for s in definition["steps"]}
    assert steps["nuclei"]["args"]["timeout"] >= 900
    assert steps["commix"]["args"]["timeout"] >= 900
