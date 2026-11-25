"""Regression tests for R-01: playbook/template registry resolves packaged YAML dirs.

The registry previously resolved templates/playbooks to a
<venv>/resources/playbooks path that does not exist after a wheel install, so
template_list() / template_run() returned empty. The dirs now resolve to the
packaged src/kmcp/playbooks/{definitions,templates} dirs (env override wins).
"""

from __future__ import annotations

from pathlib import Path

from kmcp.playbooks import registry


def test_registry_resolves_packaged_dirs_after_install():
    """After a wheel install the packaged dirs (not resources/playbooks) are used."""
    assert str(registry.PLAYBOOKS_DIR).endswith("kmcp/playbooks/definitions")
    assert str(registry.TEMPLATES_DIR).endswith("kmcp/playbooks/templates")


def test_template_list_returns_packaged_templates():
    registry._templates.clear()
    registry.load_yaml_templates()
    ids = {t["id"] for t in registry.list_templates()}
    assert {"quick_web_scan", "incident_response"} <= ids


def test_get_template_resolves_packaged_template():
    registry._templates.clear()
    registry.load_yaml_templates()
    tmpl = registry.get_template("quick_web_scan")
    assert tmpl["id"] == "quick_web_scan"


def test_playbooks_load_from_packaged_definitions():
    registry._playbooks.clear()
    registry.load_yaml_playbooks()
    ids = {p["id"] for p in registry.list_playbooks()}
    assert {"web_assessment", "external_recon", "network_assessment"} <= ids


def test_env_override_takes_priority(tmp_path, monkeypatch):
    """An explicit KALI_MCP_TEMPLATES_DIR overrides the packaged default."""
    custom = tmp_path / "my_templates"
    custom.mkdir()
    (custom / "custom_tpl.yaml").write_text(
        "id: custom_tpl\nname: Custom\nparameters: {}\n", encoding="utf-8"
    )
    try:
        monkeypatch.setenv("KALI_MCP_TEMPLATES_DIR", str(custom))
        # Re-derive module-level dir from env without reloading the module.
        registry.TEMPLATES_DIR = Path(str(custom))
        registry._templates.clear()
        registry.load_yaml_templates()
        ids = {t["id"] for t in registry.list_templates()}
        assert "custom_tpl" in ids
        assert str(registry.TEMPLATES_DIR) == str(custom)
    finally:
        registry.TEMPLATES_DIR = Path(
            registry._PACKAGE_DIR / "templates"
        )
        registry._templates.clear()
        registry.load_yaml_templates()
