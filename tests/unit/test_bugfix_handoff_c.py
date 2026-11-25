"""Regression tests for builder handoff C: packaging + built-in playbook wiring.

Covers F-02 (YAML playbooks ship in wheels/sdists), F-04 (no unresolved options
placeholders or schema fragments in command args) and F-05 (port-scan, fuzzing,
timeout and failure-handling wiring). All checks are static: YAML/config files
are parsed from disk, no tools or network are invoked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = REPO_ROOT / "src" / "kmcp" / "playbooks" / "definitions"
RESOURCES = REPO_ROOT / "resources" / "playbooks"

MIRRORED_PLAYBOOKS = (
    "web_assessment",
    "network_assessment",
    "external_recon",
    "ad_assessment",
    "api_assessment",
    "vulnerability_validation",
    "custom_recon",
    "web_fuzz",
)


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"{path}: expected a mapping"
    return data


def steps_by_id(definition: dict) -> dict[str, dict]:
    return {step["id"]: step for step in definition["steps"]}


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


# ── F-02: packaging ───────────────────────────────────────────────────────


def _pyproject_section(header: str) -> str:
    """Return the body of a TOML section (stdlib-only, Python 3.10 compatible)."""
    lines: list[str] = []
    inside = False
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("["):
            inside = line.strip() == header
        elif inside:
            lines.append(line)
    return "\n".join(lines)


def test_package_data_includes_playbook_yaml():
    # Avoid tomllib (Python 3.11+) because CI still runs Python 3.10.
    package_data = _pyproject_section("[tool.setuptools.package-data]")
    assert '"studio/web/**/*"' in package_data
    assert '"playbooks/**/*.yaml"' in package_data


def test_manifest_in_includes_playbook_yaml():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert re.search(r"^graft src/kmcp/playbooks\s*$", manifest, re.MULTILINE)
    assert re.search(r"^include \*\.yaml\s*$", manifest, re.MULTILINE)


@pytest.mark.parametrize("playbook_id", MIRRORED_PLAYBOOKS)
def test_required_playbook_yaml_present_in_both_trees(playbook_id):
    src = DEFINITIONS / f"{playbook_id}.yaml"
    res = RESOURCES / f"{playbook_id}.yaml"
    assert src.is_file(), f"missing {src}"
    assert res.is_file(), f"missing {res}"
    assert load_yaml(src)["id"] == playbook_id
    assert load_yaml(res)["id"] == playbook_id


@pytest.mark.parametrize("playbook_id", MIRRORED_PLAYBOOKS)
def test_mirrors_are_byte_consistent(playbook_id):
    src = (DEFINITIONS / f"{playbook_id}.yaml").read_bytes()
    res = (RESOURCES / f"{playbook_id}.yaml").read_bytes()
    assert src == res, f"{playbook_id}: definitions/resources mirrors diverged"


@pytest.mark.parametrize("playbook_id", MIRRORED_PLAYBOOKS)
def test_yaml_in_both_trees_is_parseable(playbook_id):
    for path in (DEFINITIONS / f"{playbook_id}.yaml", RESOURCES / f"{playbook_id}.yaml"):
        definition = load_yaml(path)
        assert definition.get("steps"), f"{path}: no steps"
        assert len({step["id"] for step in definition["steps"]}) == len(definition["steps"])


# ── F-04: concrete defaults, no schema fragments in args ──────────────────


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
@pytest.mark.parametrize("playbook_id", ("external_recon", "network_assessment"))
def test_ports_have_concrete_default(mirror, playbook_id):
    definition = load_yaml(mirror / f"{playbook_id}.yaml")
    options = definition["inputs"]["options"]
    assert options.get("default", {}).get("ports") == "1-1000"
    assert options["properties"]["ports"]["default"] == "1-1000"


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
def test_no_unresolved_options_placeholders_in_step_args(mirror):
    """Builtin/studio runs only substitute ``{{target}}`` in step args; any
    other placeholder leaks verbatim into a tool command line."""
    for playbook_id in ("external_recon", "network_assessment", "web_assessment"):
        path = mirror / f"{playbook_id}.yaml"
        for step in load_yaml(path)["steps"]:
            for source in ("args", "inputs"):
                section = step.get(source)
                if not isinstance(section, dict):
                    continue
                for text in _walk_strings(section):
                    # `options.domain` is the deliberate domain-only fallback,
                    # guarded by a `when` condition that skips the step.
                    if "options.domain" in text:
                        assert "options.domain" in str(step.get("when", "")), (
                            f"{path.name}/{step['id']}: unguarded options.domain placeholder"
                        )
                        continue
                    assert not re.search(r"\{\{\s*(inputs\.)?options\.", text), (
                        f"{path.name}/{step['id']}: unresolved options placeholder in {source}"
                    )
                    assert "description" not in text, (
                        f"{path.name}/{step['id']}: schema fragment in {source}"
                    )


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
def test_port_scan_args_are_concrete(mirror):
    definition = load_yaml(mirror / "external_recon.yaml")
    steps = steps_by_id(definition)
    ports_step = steps["ports"]
    assert ports_step["module"] == "nmap"  # masscan rejects hostname targets
    assert ports_step["args"]["ports"] == "1-1000"
    assert ports_step["args"]["extra_args"] == ""

    network = steps_by_id(load_yaml(mirror / "network_assessment.yaml"))
    for step_id in ("discover", "ports", "enumerate"):
        assert network[step_id]["args"]["ports"] == "1-1000"
    assert network["ports"]["args"]["rate"] == "500"


# ── F-05: DAG step wiring ─────────────────────────────────────────────────


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
def test_external_recon_port_scan_and_subfinder_target(mirror):
    definition = load_yaml(mirror / "external_recon.yaml")
    steps = steps_by_id(definition)

    # A port scan step must always carry a concrete port spec; masscan in
    # particular requires `-p <ports>` and cannot accept hostname targets.
    for step in definition["steps"]:
        module = step.get("module") or step.get("tool")
        if module in ("masscan", "nmap"):
            assert step["args"].get("ports"), f"{step['id']}: missing port spec"

    discover = steps["discover"]
    assert discover["module"] == "subfinder"
    assert discover["args"]["domain"] != "{{target}}"
    assert "options.domain" in discover["args"]["domain"]
    assert "options.domain" in str(discover.get("when", ""))


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
@pytest.mark.parametrize("playbook_id", ("web_assessment", "web_fuzz"))
def test_ffuf_targets_contain_fuzz_placeholder(mirror, playbook_id):
    definition = load_yaml(mirror / f"{playbook_id}.yaml")
    ffuf_steps = [
        step
        for step in definition["steps"]
        if step.get("tool") == "ffuf" or step.get("module") == "ffuf"
    ]
    assert ffuf_steps, f"{playbook_id}: no ffuf step"
    for step in ffuf_steps:
        assert "FUZZ" in step["args"]["target"], f"{playbook_id}/{step['id']}: missing FUZZ"


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
def test_web_assessment_nuclei_timeout_raised(mirror):
    definition = load_yaml(mirror / "web_assessment.yaml")
    steps = steps_by_id(definition)
    assert steps["vulnerability_checks"]["module"] == "nuclei"
    assert int(steps["vulnerability_checks"].get("timeout", 0)) >= 600
    commix = next((s for s in definition["steps"] if "commix" in s["id"]), None)
    if commix is not None:  # no commix step is defined in these YAML files
        assert int(commix.get("timeout", 0)) >= 600


@pytest.mark.parametrize("mirror", (DEFINITIONS, RESOURCES))
def test_ad_enum_failure_is_non_fatal(mirror):
    definition = load_yaml(mirror / "ad_assessment.yaml")
    steps = steps_by_id(definition)
    assert steps["enum"]["module"] == "enum4linux"
    assert steps["enum"]["on_failure"] == "continue"
    for step_id in ("bloodhound", "netexec", "crackmapexec", "correlate", "report"):
        assert step_id in steps
    assert "enum" in steps["bloodhound"]["depends_on"]
    assert "bloodhound" in steps["netexec"]["depends_on"]
    assert "correlate" in steps["report"]["depends_on"]
