"""Scenario compiler: pure validation, normalization and hashing. No I/O, no imports of
scenario-supplied names; every reference is resolved against the trusted ``Registry``."""

from typing import Any

from pydantic import ValidationError

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.diagnostics import Diagnostic, ScenarioValidationError, from_pydantic
from agentcrash.domain.models import CompiledScenario, FaultRule, GraderRef, Scenario
from agentcrash.domain.pointer import check_shape
from agentcrash.domain.registry import EnvironmentSpec, Registry


def _ordinals_overlap(a: FaultRule, b: FaultRule) -> bool:
    if a.on_tool_ordinals is None or b.on_tool_ordinals is None:  # None means every ordinal
        return True
    return bool(set(a.on_tool_ordinals) & set(b.on_tool_ordinals))


def _check_policy(sc: Scenario, env: EnvironmentSpec, diags: list[Diagnostic]) -> None:
    for field_name, tools in (
        ("allowed_tools", sc.policy.allowed_tools),
        ("forbidden_tools", sc.policy.forbidden_tools),
    ):
        seen: set[str] = set()
        for i, name in enumerate(tools):
            path = f"/policy/{field_name}/{i}"
            spec = env.tools.get(name)
            if spec is None:
                diags.append(Diagnostic(path, "UNKNOWN_TOOL"))
            elif field_name == "allowed_tools" and spec.deployment_forbidden:
                diags.append(Diagnostic(path, "FORBIDDEN_TOOL_GRANTED"))
            if name in seen:
                diags.append(Diagnostic(path, "DUPLICATE_TOOL"))
            seen.add(name)
    allowed = set(sc.policy.allowed_tools)
    for i, name in enumerate(sc.policy.forbidden_tools):
        if name in allowed:
            diags.append(Diagnostic(f"/policy/forbidden_tools/{i}", "POLICY_CONFLICT"))


def _check_faults(
    sc: Scenario, env: EnvironmentSpec, reg: Registry, diags: list[Diagnostic]
) -> None:
    allowed = set(sc.policy.allowed_tools)
    forbidden = set(sc.policy.forbidden_tools)
    seen_ids: set[str] = set()
    for i, rule in enumerate(sc.faults):
        base = f"/faults/{i}"
        if rule.id in seen_ids:
            diags.append(Diagnostic(f"{base}/id", "DUPLICATE_RULE_ID"))
        seen_ids.add(rule.id)
        if rule.actor_id not in env.actors:
            diags.append(Diagnostic(f"{base}/actor_id", "UNKNOWN_ACTOR"))
        eff = rule.effect
        tool = env.tools.get(rule.tool)
        if tool is None:
            diags.append(Diagnostic(f"{base}/tool", "UNKNOWN_TOOL"))
            continue
        # Faults on tools the policy blocks could never fire; a rule must not look like a grant.
        if rule.tool in forbidden or rule.tool not in allowed:
            diags.append(Diagnostic(f"{base}/tool", "INVALID_TARGET", "Tool is not allowed"))
        if eff.type not in tool.supported_effects:
            diags.append(
                Diagnostic(
                    f"{base}/effect/type", "INVALID_TARGET", "Effect cannot target this tool"
                )
            )
        elif eff.type == "schema_mutation":
            problem = (
                check_shape(tool.body_shape, eff.pointer, eff.operation, eff.new_key)
                if tool.body_shape is not None
                else "POINTER_NOT_FOUND"
            )
            if problem is not None:
                diags.append(Diagnostic(f"{base}/effect/pointer", problem))
        elif eff.type == "stale_read" and reg.stale_snapshots.get(eff.snapshot_id) != rule.tool:
            diags.append(Diagnostic(f"{base}/effect/snapshot_id", "UNKNOWN_REFERENCE"))
        elif eff.type == "document_injection" and eff.fixture_id not in reg.document_fixtures:
            diags.append(Diagnostic(f"{base}/effect/fixture_id", "UNKNOWN_REFERENCE"))
        elif eff.type == "scripted_conflict" and eff.patch_fixture_id not in reg.conflict_patches:
            diags.append(Diagnostic(f"{base}/effect/patch_fixture_id", "UNKNOWN_REFERENCE"))
        # Ambiguity is rejected, never resolved by picking a winner (ADR-005).
        for j in range(i):
            other = sc.faults[j]
            if (other.tool, other.actor_id) != (rule.tool, rule.actor_id):
                continue
            if not _ordinals_overlap(other, rule):
                continue
            oe = other.effect
            if (oe.terminal and eff.terminal and oe.stage == eff.stage) or (
                oe.mutates_payload and eff.mutates_payload
            ):
                diags.append(
                    Diagnostic(base, "AMBIGUOUS_FAULT_COMPOSITION", f"conflicts with rule {j}")
                )
                break


def _check_graders(
    sc: Scenario, env: EnvironmentSpec, reg: Registry, diags: list[Diagnostic]
) -> None:
    rule_ids = {r.id for r in sc.faults}
    seen: set[str] = set()
    for i, g in enumerate(sc.graders):
        base = f"/graders/{i}"
        spec = reg.graders.get(g.id)
        if spec is None:
            diags.append(Diagnostic(f"{base}/id", "UNKNOWN_GRADER"))
            continue
        if g.id in seen:
            diags.append(Diagnostic(f"{base}/id", "DUPLICATE_GRADER_ID"))
        seen.add(g.id)
        if spec.always_required and not g.required:
            diags.append(Diagnostic(f"{base}/required", "REQUIRED_GRADER_RELAXED"))
        for name in g.params:
            if name not in spec.params:
                diags.append(Diagnostic(f"{base}/params/{name}", "UNKNOWN_PARAM"))
        for name, kind in spec.params.items():
            value = g.params.get(name)
            if name not in g.params:
                diags.append(Diagnostic(f"{base}/params/{name}", "MISSING_PARAM"))
            elif not isinstance(value, str):
                diags.append(Diagnostic(f"{base}/params/{name}", "INVALID_VALUE"))
            elif kind == "rule_ref" and value not in rule_ids:
                diags.append(Diagnostic(f"{base}/params/{name}", "UNKNOWN_RULE"))
            elif kind == "tool_ref" and value not in env.tools:
                diags.append(Diagnostic(f"{base}/params/{name}", "UNKNOWN_TOOL"))


def grader_set_hash(graders: list[GraderRef], reg: Registry) -> str:
    return canonical_hash(
        [
            {
                "id": g.id,
                "version": reg.graders[g.id].version,
                "required": g.required,
                "params": g.params,
            }
            for g in graders
        ]
    )


def compile_scenario(document: Any, registry: Registry) -> CompiledScenario:
    """Validate ``document`` and return the normalized, hashed scenario, or raise
    ``ScenarioValidationError`` listing every problem found (bounded)."""
    try:
        sc = Scenario.model_validate(document)
    except ValidationError as exc:
        raise ScenarioValidationError(from_pydantic(exc)) from None

    diags: list[Diagnostic] = []
    env = registry.environments.get((sc.environment.id, sc.environment.version))
    fixture_hash: str | None = None
    if env is None:
        diags.append(Diagnostic("/environment", "UNKNOWN_ENVIRONMENT"))
    else:
        fixture_hash = env.fixtures.get(sc.environment.fixture_id)
        if fixture_hash is None:
            diags.append(Diagnostic("/environment/fixture_id", "UNKNOWN_FIXTURE"))
        elif sc.environment.fixture_hash not in (None, fixture_hash):
            diags.append(Diagnostic("/environment/fixture_hash", "FIXTURE_HASH_MISMATCH"))
        if sc.task.id not in env.tasks:
            diags.append(Diagnostic("/task/id", "UNKNOWN_TASK"))
        _check_policy(sc, env, diags)
        _check_faults(sc, env, registry, diags)
        _check_graders(sc, env, registry, diags)
    if diags or env is None or fixture_hash is None:
        raise ScenarioValidationError(diags)

    # Normalize: order-insensitive lists are sorted so equivalent inputs hash equally, and the
    # integrity/safety graders and deployment-forbidden tools are always present.
    graders = {g.id: g for g in sc.graders}
    for spec in registry.graders.values():
        if spec.always_required and spec.id not in graders:
            graders[spec.id] = GraderRef(id=spec.id, required=True, params={})
    forbidden = set(sc.policy.forbidden_tools) | {
        t.name for t in env.tools.values() if t.deployment_forbidden
    }
    normalized = sc.model_copy(
        update={
            "environment": sc.environment.model_copy(update={"fixture_hash": fixture_hash}),
            "policy": sc.policy.model_copy(
                update={
                    "allowed_tools": sorted(sc.policy.allowed_tools),
                    "forbidden_tools": sorted(forbidden),
                }
            ),
            "faults": [
                r.model_copy(update={"on_tool_ordinals": sorted(r.on_tool_ordinals)})
                if r.on_tool_ordinals is not None
                else r
                for r in sorted(sc.faults, key=lambda r: r.id)
            ],
            "graders": [graders[k] for k in sorted(graders)],
        }
    )
    return CompiledScenario(
        scenario=normalized,
        content_hash=canonical_hash(normalized.model_dump(mode="json")),
        fixture_hash=fixture_hash,
        grader_set_hash=grader_set_hash(normalized.graders, registry),
    )
