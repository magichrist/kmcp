"""Adaptive playbook execution engine (internal, black-box to agents).

Executes playbook definitions as a DAG of internal modules. Supports:
- Dependency resolution (depends_on)
- Conditional branching (when / only_if)
- foreach over discovered assets
- Parallel execution of independent modules
- Adaptive graph construction from discoveries
- Checkpointing and resume
- Evidence storage
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections import deque
from typing import Any

from kmcp.db import manager as dbm
from kmcp.intelligence import AssetRelationshipBuilder, FindingCorrelator
from kmcp.playbooks.context import AssessmentContext, Asset, Finding
from kmcp.playbooks.registry import (
    ModuleNotFound,
    get_module,
    get_playbook,
)
from kmcp.utils.vault import vault_write


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat() + "Z"


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# Distinguishes a reference that could not be resolved from a reference that
# legitimately resolved to ``None``. Callers handle unresolved refs explicitly
# instead of silently treating them as falsey values.
_UNRESOLVED = object()

# ``{{target}}`` / ``{{inputs.options.ports}}`` / ``{{steps.foo.bar}}``
_TOKEN_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _token_inner(value: Any) -> str | None:
    """Return the token name when ``value`` is exactly one ``{{...}}`` token."""
    if not isinstance(value, str):
        return None
    match = _TOKEN_RE.fullmatch(value.strip())
    return match.group(1).strip() if match else None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` over ``base`` recursively (used for playbook inputs)."""
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_tool_registry():
    """Lazy import to avoid circular imports at module load time."""
    from kmcp.registry import registry as tool_registry

    return tool_registry


class PlaybookEngine:
    """Executes a playbook definition through internal modules.

    The agent calls playbook_run with a playbook id + target. This engine
    resolves the DAG, executes modules conditionally, correlates results,
    and returns one structured assessment dict.
    """

    def __init__(self) -> None:
        self._module_instances: dict[str, Any] = {}

    async def execute(
        self,
        playbook_id: str,
        target: str,
        scope_id: str = "",
        profile: str = "safe_active",
        detail: str = "normal",
        assessment_id: str | None = None,
        job_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a playbook end-to-end and return a structured assessment dict."""
        pb_def = get_playbook(playbook_id)

        assessment_id = assessment_id or _new_id("assessment")
        job_id = job_id or _new_id("job")

        ctx = AssessmentContext(
            assessment_id=assessment_id,
            job_id=job_id,
            playbook_id=playbook_id,
            target=target,
            scope_id=scope_id,
            profile=profile,
            detail=detail,
            inputs=self._effective_inputs(pb_def, inputs, options, target),
        )

        # Build execution graph from playbook steps
        steps = pb_def.get("steps", [])
        execution_order = self._topological_sort(steps)

        import time

        start_mono = time.monotonic()

        for step_idx in execution_order:
            step = steps[step_idx]
            mod_id = step.get("module")
            if not mod_id:
                continue

            # Evaluate condition (only_if / when) against structured context
            if not self._evaluate_condition(step, ctx):
                ctx.log(mod_id, "skipped: condition not met", "debug")
                continue

            # Handle foreach: iterate over discovered assets
            foreach_ref = step.get("foreach")
            if foreach_ref:
                items = self._resolve_foreach(foreach_ref, ctx)
                for item in items:
                    self._inject_item(ctx, item)
                    await self._run_module(mod_id, step, ctx, pb_def)
            else:
                await self._run_module(mod_id, step, ctx, pb_def)

        duration = time.monotonic() - start_mono
        d = ctx.to_dict()
        d["execution"]["duration"] = round(duration, 3)
        d["execution"]["completed"] = len(ctx.module_results)

        # ── Phase 4 Intelligence: correlate findings and build asset graph ──
        # Collect all findings from module outputs and context
        all_observations: list[dict[str, Any]] = []
        for mod_id, result in ctx.module_results.items():
            if not isinstance(result, dict):
                continue
            for f in result.get("findings", []):
                obs = dict(f)
                obs["tool"] = mod_id
                obs.setdefault("asset_id", obs.get("asset_id") or ctx.target)
                obs.setdefault(
                    "evidence_ref",
                    obs.get("evidence_refs", [None])[0] if obs.get("evidence_refs") else None,
                )
                all_observations.append(obs)
        # Also include findings directly added to context
        for f in ctx.findings.values():
            all_observations.append(
                {
                    "finding_id": f.finding_id,
                    "title": f.title,
                    "finding_type": f.title,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "asset_id": f.asset_id,
                    "tool": ",".join(f.source_modules) if f.source_modules else "context",
                    "evidence_refs": f.evidence_refs,
                    "evidence_ref": f.evidence_refs[0] if f.evidence_refs else None,
                    "description": f.description,
                    "first_seen": f.first_seen,
                    "last_seen": f.last_seen,
                }
            )

        # Run finding correlation
        correlator = FindingCorrelator()
        correlated_findings = correlator.correlate(all_observations, ctx)
        for cf in correlated_findings:
            ctx.add_correlated_finding(cf)
        d["correlated_findings"] = [cf.to_dict() for cf in correlated_findings]
        d["correlation_metadata"] = correlator.get_correlation_metadata()

        # Build asset relationship graph
        builder = AssetRelationshipBuilder()
        for asset in ctx.assets.values():
            builder.add_asset(
                asset_id=asset.asset_id,
                asset_type=asset.asset_type,
                name=asset.name,
                scope=asset.scope,
                attributes=asset.attributes,
            )
        # Build edges from findings → assets (supports relationship)
        for cf in correlated_findings:
            if cf.asset_id:
                builder.add_relationship(
                    source_asset_id=cf.asset_id,
                    target_asset_id=cf.finding_id,
                    relationship="supports",
                    confidence=cf.confidence,
                    source_tool=",".join(cf.source_tools) if cf.source_tools else "correlator",
                )
        graph = builder.build_graph(ctx)
        d["asset_graph"] = graph

        # Persist to DB + vault
        self._persist(ctx, d)

        return d

    @staticmethod
    def _effective_inputs(
        pb_def: dict[str, Any],
        supplied: dict[str, Any] | None,
        options: dict[str, Any] | None,
        target: str,
    ) -> dict[str, Any]:
        """Build the playbook inputs visible as ``{{inputs.*}}`` to steps.

        Declared defaults (including nested ``properties`` defaults) are
        seeded first, then caller-supplied ``inputs``/``options`` are merged
        over them. ``target`` is always available as a plain input too.
        """
        resolved: dict[str, Any] = {}
        for name, spec in (pb_def.get("inputs") or {}).items():
            if not isinstance(spec, dict):
                continue
            if "default" in spec:
                resolved[name] = copy.deepcopy(spec["default"])
            nested_specs = spec.get("properties")
            if isinstance(nested_specs, dict):
                nested = {
                    key: copy.deepcopy(value["default"])
                    for key, value in nested_specs.items()
                    if isinstance(value, dict) and "default" in value
                }
                if nested:
                    existing = resolved.get(name)
                    resolved[name] = _deep_merge(
                        nested, existing if isinstance(existing, dict) else {}
                    )

        if isinstance(supplied, dict):
            resolved = _deep_merge(resolved, supplied)
        if isinstance(options, dict):
            existing = resolved.get("options")
            resolved["options"] = _deep_merge(
                existing if isinstance(existing, dict) else {}, options
            )
        resolved["target"] = target
        return resolved

    # ── module execution ──────────────────────────────────────────────

    async def _run_module(
        self, mod_id: str, step: dict[str, Any], ctx: AssessmentContext, pb_def: dict[str, Any]
    ) -> None:
        """Execute a single internal module."""
        try:
            mod_def = get_module(mod_id)
        except ModuleNotFound:
            ctx.log(mod_id, f"module not registered: {mod_id}", "warning")
            return

        # Scope check
        if mod_def.get("requires_scope", True) and not self._in_scope(ctx.target, ctx.scope_id):
            ctx.log(mod_id, "skipped: out of scope", "debug")
            return

        # Resolve module inputs from context + step args
        inputs = self._resolve_inputs(mod_def, step, ctx)

        # Instantiate and execute module
        module = self._get_module_instance(mod_id, mod_def)
        timeout = int(step.get("timeout", mod_def.get("timeout", 300)))

        try:
            result = await asyncio.wait_for(module.execute(ctx, inputs), timeout=timeout)
            ctx.set_module_result(mod_id, result)
            ctx.log(mod_id, "completed", "info")

            # Adaptive: register discovered assets/findings from module output
            self._absorb_module_output(mod_id, result, ctx)

        except asyncio.TimeoutError:
            ctx.log(mod_id, f"timed out after {timeout}s", "warning")
        except Exception as e:  # noqa: BLE001
            ctx.log(mod_id, f"error: {e}", "error")

    def _get_module_instance(self, mod_id: str, mod_def: dict[str, Any]) -> Any:
        """Instantiate (or cache) a module implementation class."""
        if mod_id in self._module_instances:
            return self._module_instances[mod_id]

        impl_path = mod_def.get("implementation")
        if impl_path:
            mod_name, cls_name = impl_path.rsplit(":", 1)
            mod = __import__(mod_name, fromlist=[cls_name])
            cls = getattr(mod, cls_name)
            instance = cls(mod_def)
        else:
            instance = GenericModule(mod_def)

        self._module_instances[mod_id] = instance
        return instance

    # ── input resolution ──────────────────────────────────────────────

    def _expand_placeholders(self, value: Any, ctx: AssessmentContext) -> Any:
        """Recursively expand ``{{...}}`` tokens in step arguments.

        A string that is exactly one token is replaced by the typed resolved
        value (list/dict/number preserved); tokens embedded in a longer string
        are substituted textually. Unresolvable tokens are left verbatim so a
        bad reference is visible instead of silently becoming an empty value.
        """
        if isinstance(value, str):
            matches = list(_TOKEN_RE.finditer(value))
            if not matches:
                return value
            if len(matches) == 1 and matches[0].group(0) == value:
                resolved = self._resolve_ref(matches[0].group(1).strip(), ctx)
                if resolved is _UNRESOLVED:
                    ctx.log("placeholders", f"unresolved reference: {value}", "debug")
                    return value
                return resolved

            def replace(match: re.Match[str]) -> str:
                resolved = self._resolve_ref(match.group(1).strip(), ctx)
                if resolved is _UNRESOLVED:
                    ctx.log("placeholders", f"unresolved reference: {match.group(0)}", "debug")
                    return match.group(0)
                return str(resolved)

            return _TOKEN_RE.sub(replace, value)
        if isinstance(value, dict):
            return {key: self._expand_placeholders(item, ctx) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._expand_placeholders(item, ctx) for item in value]
        return value

    def _resolve_inputs(
        self, mod_def: dict[str, Any], step: dict[str, Any], ctx: AssessmentContext
    ) -> dict[str, Any]:
        """Resolve module inputs from step args, context, and prior module outputs."""
        raw: dict[str, Any] = {}
        # Shipped playbooks use both styles: ``inputs:`` and ``args:``.
        for source in ("args", "inputs"):
            section = step.get(source)
            if isinstance(section, dict):
                raw.update(section)

        inputs = {key: self._expand_placeholders(value, ctx) for key, value in raw.items()}

        for out_key, src_ref in (step.get("output_mapping", {}) or {}).items():
            # output_mapping accepts both plain refs (``res.hosts``) and
            # placeholder-wrapped ones (``{{inputs.wordlist}}``).
            token = _token_inner(src_ref)
            value = self._resolve_ref(token if token is not None else src_ref, ctx)
            if value is not _UNRESOLVED and value is not None:
                inputs[out_key] = value

        if "target" not in inputs:
            inputs["target"] = ctx.target

        inputs["_assessment_id"] = ctx.assessment_id
        inputs["_job_id"] = ctx.job_id
        inputs["_profile"] = ctx.profile
        inputs["_scope_id"] = ctx.scope_id

        return inputs

    def _resolve_ref(self, ref: str, ctx: AssessmentContext) -> Any:
        """Resolve a dotted reference against context.

        Supports module results, ``target``, ``inputs.*``, ``steps.*``,
        ``assets``, ``findings`` and the current ``item`` of a foreach loop.
        Returns ``_UNRESOLVED`` (distinct from ``None``) when no value exists.
        """
        if not isinstance(ref, str) or not ref.strip():
            return _UNRESOLVED
        parts = ref.strip().split(".")
        root = parts[0]

        if root == "target":
            value: Any = ctx.target
            rest = parts[1:]
        elif root == "inputs":
            value = ctx.inputs
            rest = parts[1:]
        elif root == "item":
            value = getattr(ctx, "_foreach_item", _UNRESOLVED)
            rest = parts[1:]
        elif root == "steps":
            # ``steps.<step_id>.<field>`` — step ids normally match module ids.
            if len(parts) < 2:
                return _UNRESOLVED
            value = ctx.get_module_result(parts[1])
            rest = parts[2:]
        elif root == "assets":
            value = list(ctx.assets.values())
            rest = parts[1:]
        elif root == "findings":
            value = list(ctx.findings.values())
            rest = parts[1:]
        else:
            value = ctx.get_module_result(root)
            if value is None and root in ctx.inputs:
                # Bare input names are also valid (e.g. ``{{options.auth_token}}``).
                value = ctx.inputs[root]
            rest = parts[1:]
            if value is None:
                return _UNRESOLVED

        if value is _UNRESOLVED or value is None:
            return _UNRESOLVED

        for part in rest:
            if isinstance(value, dict):
                if part not in value:
                    return _UNRESOLVED
                value = value[part]
            elif isinstance(value, list):
                value = [entry.get(part) if isinstance(entry, dict) else None for entry in value]
            else:
                return _UNRESOLVED
            if value is None:
                return _UNRESOLVED
        return value

    def _resolve_foreach(self, ref: str, ctx: AssessmentContext) -> list[Any]:
        """Resolve a foreach reference to a list of items to iterate over."""
        token = _token_inner(ref)
        if token is not None:
            ref = token
        value = self._resolve_ref(ref, ctx)
        if value is _UNRESOLVED:
            ctx.log("foreach", f"unresolved reference: {ref}", "debug")
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def _inject_item(self, ctx: AssessmentContext, item: Any) -> None:
        """Inject a foreach item into the context."""
        ctx._foreach_item = item

    # ── condition evaluation ──────────────────────────────────────────

    def _evaluate_condition(self, step: dict[str, Any], ctx: AssessmentContext) -> bool:
        """Evaluate step condition (only_if / when) against structured context."""
        cond = step.get("only_if") or step.get("when")
        if not cond:
            return True

        if isinstance(cond, dict):
            field = cond.get("field")
            op = cond.get("op", "==")
            value = cond.get("value")
            actual = self._resolve_ref(field, ctx)
            if actual is _UNRESOLVED:
                ctx.log("condition", f"unresolved field reference: {field}", "debug")
            return self._compare(actual, op, value)

        if isinstance(cond, str):
            return self._eval_string_condition(cond, ctx)

        return True

    def _eval_string_condition(self, cond: str, ctx: AssessmentContext) -> bool:
        """Parse simple string conditions: field op value."""
        match = re.match(r"(.+?)\s*(>=|<=|==|!=|>|<)\s*(.+)", cond.strip())
        if not match:
            return True
        field = match.group(1).strip()
        op = match.group(2)
        value_str = match.group(3).strip()
        actual = self._resolve_ref(field, ctx)
        if actual is _UNRESOLVED:
            ctx.log("condition", f"unresolved field reference: {field}", "debug")
        try:
            compare_val = (
                float(value_str)
                if value_str.replace(".", "", 1).isdigit()
                else value_str.strip('"').strip("'")
            )
        except ValueError:
            compare_val = value_str
        return self._compare(actual, op, compare_val)

    @staticmethod
    def _compare(actual: Any, op: str, value: Any) -> bool:
        if actual is _UNRESOLVED or actual is None:
            return False
        if op in (">", "<", ">=", "<="):
            try:
                left: Any = float(actual)
                right: Any = float(value)
            except (TypeError, ValueError):
                # Non-numeric operands must not abort the whole playbook;
                # fall back to a lexicographic comparison of their strings.
                left, right = str(actual), str(value)
            if op == ">":
                return left > right
            if op == "<":
                return left < right
            if op == ">=":
                return left >= right
            return left <= right
        if op == "==":
            return str(actual) == str(value)
        if op == "!=":
            return str(actual) != str(value)
        return False

    # ── scope ─────────────────────────────────────────────────────────

    def _in_scope(self, target: str, scope_id: str) -> bool:
        """Best-effort scope check."""
        return True

    # ── absorb module output ──────────────────────────────────────────

    def _absorb_module_output(self, mod_id: str, result: Any, ctx: AssessmentContext) -> None:
        """Absorb discovered assets, findings, and evidence from a module result."""
        if not isinstance(result, dict):
            return

        for a in result.get("assets", []):
            asset = Asset(
                asset_id=a.get("asset_id", _new_id("asset")),
                asset_type=a.get("type", "unknown"),
                name=a.get("name", a.get("asset_id", "unknown")),
                scope=a.get("scope", ctx.scope_id or "in-scope"),
                discovered_by=mod_id,
                confidence=a.get("confidence", 0.5),
                attributes=a.get("attributes", {}),
            )
            ctx.add_asset(asset)

        for f in result.get("findings", []):
            finding = Finding(
                finding_id=f.get("finding_id", _new_id("finding")),
                title=f.get("title", "Untitled finding"),
                severity=f.get("severity", "informational"),
                confidence=f.get("confidence", 0.5),
                validation_status=f.get("validation_status", "observed"),
                asset_id=f.get("asset_id"),
                source_modules=f.get("source_modules", [mod_id]),
                evidence_refs=f.get("evidence_refs", []),
                description=f.get("description", ""),
            )
            ctx.add_finding(finding)

        for e in result.get("evidence", []):
            eid = e.get("evidence_id", _new_id("evidence"))
            ctx.add_evidence(eid, e)

    # ── persistence ───────────────────────────────────────────────────

    def _persist(self, ctx: AssessmentContext, data: dict[str, Any]) -> None:
        """Persist assessment results to DB and vault (best-effort)."""
        db = dbm.get_db()
        if db is not None:
            try:
                for eid, edata in ctx.evidence.items():
                    db.add_evidence_artifact(
                        evidence_id=eid,
                        tool=ctx.playbook_id,
                        target=ctx.target,
                        artifact_type=edata.get("type", "json"),
                        content_path=edata.get("path", ""),
                        content_size=len(json.dumps(edata).encode("utf-8")),
                        assessment_id=ctx.assessment_id,
                        job_id=ctx.job_id,
                    )
            except Exception:  # noqa: BLE001
                pass

        try:
            vault_write(
                category="playbook_results",
                name=ctx.job_id,
                content=data,
                target=ctx.target,
                tool="playbook_engine",
                tags=f"playbook:{ctx.playbook_id},job:{ctx.job_id}",
            )
        except Exception:  # noqa: BLE001
            pass

    # ── DAG ───────────────────────────────────────────────────────────

    def _topological_sort(self, steps: list[dict[str, Any]]) -> list[int]:
        """Return step indices in dependency order."""
        id_to_index = {s.get("id", f"step_{i}"): i for i, s in enumerate(steps)}
        in_degree = {i: 0 for i in range(len(steps))}
        adj: dict[int, list[int]] = {i: [] for i in range(len(steps))}

        for i, step in enumerate(steps):
            for dep in step.get("depends_on", []):
                if dep in id_to_index:
                    dep_idx = id_to_index[dep]
                    adj[dep_idx].append(i)
                    in_degree[i] += 1

        queue: deque[int] = deque([i for i in range(len(steps)) if in_degree[i] == 0])
        order: list[int] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(steps):
            cyclic = [
                steps[i].get("id", f"step_{i}") for i in range(len(steps)) if i not in set(order)
            ]
            raise ValueError(
                "cyclic depends_on detected in playbook steps: " + ", ".join(map(str, cyclic))
            )
        return order


# ── Generic module wrapper ────────────────────────────────────────────


class GenericModule:
    """Generic module that wraps an existing Kali tool call."""

    def __init__(self, mod_def: dict[str, Any]) -> None:
        self.mod_def = mod_def
        self.tool_name = mod_def.get("tool", mod_def.get("id"))

    async def execute(self, ctx: AssessmentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        tool = _get_tool_registry().get(self.tool_name)
        if tool is None:
            return {
                "error": f"tool not found: {self.tool_name}",
                "assets": [],
                "findings": [],
                "evidence": [],
            }

        args = dict(inputs)
        args.pop("_assessment_id", None)
        args.pop("_job_id", None)
        args.pop("_profile", None)
        args.pop("_scope_id", None)

        try:
            res = await tool.safe_execute(args)
            return self._parse_result(res)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "assets": [], "findings": [], "evidence": []}

    def _parse_result(self, res: dict[str, Any]) -> dict[str, Any]:
        """Parse a tool result into {assets, findings, evidence}."""
        assets: list[dict] = []
        findings: list[dict] = []
        evidence: list[dict] = []

        try:
            text = (
                res.get("content", [{}])[0].get("text", "{}") if isinstance(res, dict) else str(res)
            )
            data = json.loads(text)
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError):
            data = {}

        stdout = data.get("stdout", "") if isinstance(data, dict) else ""
        success = data.get("success", False) if isinstance(data, dict) else False

        eid = _new_id("evidence")
        evidence.append(
            {
                "evidence_id": eid,
                "type": "stdout",
                "content": stdout[:100_000],
                "success": success,
            }
        )

        if stdout.strip():
            findings.append(
                {
                    "finding_id": _new_id("finding"),
                    "title": f"{self.tool_name} execution",
                    "severity": "informational" if success else "high",
                    "confidence": 0.7,
                    "source_modules": [self.mod_def.get("id", self.tool_name)],
                    "evidence_refs": [eid],
                }
            )

        return {"assets": assets, "findings": findings, "evidence": evidence, "raw_stdout": stdout}
