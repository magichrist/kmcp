"""Regression tests for R-06: legacy built-in playbook step wiring.

Covers:
- webapp ffuf target carries the FUZZ placeholder (not a bare {{target}}).
- webapp nuclei/commix steps carry an effective step budget (not the 266s
  default derived from playbook.timeout // len(steps)).
- recon skips subfinder for bare IP targets (domain-only discovery).
- AD enum4linux is non-fatal (on_failure: continue).
- YAML definitions declare genuinely-required fields (ad domain, api
  auth_token) and give optional ones (api_spec) empty defaults, so declared
  placeholders never reach tools unrendered.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kmcp.tools.playbook_ad import PlaybookAdTool
from kmcp.tools.playbook_recon import PlaybookReconTool
from kmcp.tools.playbook_webapp import PlaybookWebappTool

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = REPO_ROOT / "src" / "kmcp" / "playbooks" / "definitions"


def load_yaml(name: str) -> dict:
    with (DEFINITIONS / f"{name}.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), name
    return data


def step_by_id(playbook: dict, step_id: str) -> dict:
    for s in playbook.get("steps", []):
        if s.get("id") == step_id:
            return s
    raise AssertionError(f"step {step_id!r} not in {playbook.get('id')}")


def test_webapp_ffuf_target_carries_fuzz_placeholder():
    definition = PlaybookWebappTool()._playbook_definition(
        {"target": "http://127.0.0.1:8000/", "timeout": 2400}
    )
    ffuf = step_by_id(definition, "ffuf")
    target = ffuf["args"]["target"]
    assert target.endswith("/FUZZ"), target
    assert "//FUZZ" not in target, target


def test_webapp_nuclei_commix_have_effective_budget():
    definition = PlaybookWebappTool()._playbook_definition(
        {"target": "http://127.0.0.1:8000", "timeout": 2400}
    )
    for step_id in ("nuclei", "commix"):
        step = step_by_id(definition, step_id)
        budget = step.get("budget", {})
        assert int(budget.get("timeout", 0)) >= 900, (step_id, step)


def test_recon_skips_subfinder_for_ip_target():
    definition = PlaybookReconTool()._playbook_definition({"target": "127.0.0.1"})
    ids = [s["id"] for s in definition["steps"]]
    assert "discover" not in ids, ids
    # httpx still probes the target directly.
    resolve = step_by_id(definition, "resolve")
    assert resolve["args"]["target"] == "{{target}}"


def test_recon_includes_subfinder_for_domain_target():
    definition = PlaybookReconTool()._playbook_definition({"target": "example.com"})
    ids = [s["id"] for s in definition["steps"]]
    assert "discover" in ids, ids


def test_ad_enum4linux_is_non_fatal():
    definition = PlaybookAdTool()._playbook_definition({"target": "dc.corp.local"})
    enum = step_by_id(definition, "enum4linux")
    assert enum.get("on_failure") == "continue", enum


def test_ad_domain_is_required_input():
    ad = load_yaml("ad_assessment")
    domain = ad["inputs"]["options"]["properties"]["domain"]
    assert domain.get("required") is True, domain


def test_api_auth_token_is_required_input():
    for name in ("api_recon", "api_assessment"):
        pb = load_yaml(name)
        token = pb["inputs"]["options"]["properties"]["auth_token"]
        assert token.get("required") is True, (name, token)
        spec = pb["inputs"]["options"]["properties"]["api_spec"]
        assert spec.get("default") == "", (name, spec)
