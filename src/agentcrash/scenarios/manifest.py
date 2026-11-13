"""Run validation and immutable manifest building (CONTRACTS.md sections 4 and 7)."""

from typing import Any

from pydantic import ValidationError

from agentcrash.domain.canonical import CANONICALIZATION_VERSION, canonical_hash
from agentcrash.domain.diagnostics import Diagnostic, RunValidationError, from_pydantic
from agentcrash.domain.models import (
    FAULT_ENGINE_VERSION,
    TAXONOMY_VERSION,
    CompiledScenario,
    ExperimentCreate,
    FixtureInfo,
    Manifest,
    RedactionInfo,
    ResolvedAgent,
    ResolvedGrader,
    RunCreate,
    SourceInfo,
    ToolchainInfo,
)
from agentcrash.domain.registry import Registry


def validate_run_create(document: Any, registry: Registry) -> RunCreate:
    """Shape, caps and adapter allowlist/config. Scenario existence is checked by the caller
    against stored versions."""
    try:
        run = RunCreate.model_validate(document)
    except ValidationError as exc:
        raise RunValidationError(from_pydantic(exc)) from None
    spec = registry.agents.get((run.agent.id, run.agent.version))
    if spec is None:
        raise RunValidationError([Diagnostic("/agent/id", "UNKNOWN_ADAPTER")])
    try:
        spec.config_model.model_validate(run.agent.config)
    except ValidationError as exc:
        details = [
            Diagnostic("/agent/config" + d.path, d.code, d.message) for d in from_pydantic(exc)
        ]
        raise RunValidationError(details) from None
    return run


def validate_experiment_create(document: Any, registry: Registry) -> ExperimentCreate:
    """Shape, matrix limits and the adapter allowlist for both agents."""
    try:
        experiment = ExperimentCreate.model_validate(document)
    except ValidationError as exc:
        raise RunValidationError(from_pydantic(exc)) from None
    problems: list[Diagnostic] = []
    for field, agent in (
        ("baseline_agent", experiment.baseline_agent),
        ("candidate_agent", experiment.candidate_agent),
    ):
        spec = registry.agents.get((agent.id, agent.version))
        if spec is None:
            problems.append(Diagnostic(f"/{field}/id", "UNKNOWN_ADAPTER"))
            continue
        try:
            spec.config_model.model_validate(agent.config)
        except ValidationError as exc:
            problems += [
                Diagnostic(f"/{field}/config" + d.path, d.code, d.message)
                for d in from_pydantic(exc)
            ]
    if problems:
        raise RunValidationError(problems)
    return experiment


def build_manifest(
    compiled: CompiledScenario,
    run: RunCreate,
    registry: Registry,
    *,
    source: SourceInfo,
    toolchain: ToolchainInfo,
) -> Manifest:
    """Materialize everything the run depends on, so later registry edits cannot change it."""
    sc = compiled.scenario
    agent_spec = registry.agents[(run.agent.id, run.agent.version)]
    return Manifest(
        manifest_version="1.0",
        scenario=sc,
        scenario_hash=compiled.content_hash,
        fixture=FixtureInfo(id=sc.environment.fixture_id, hash=compiled.fixture_hash),
        agent=ResolvedAgent(
            id=run.agent.id,
            version=run.agent.version,
            config=run.agent.config,
            config_hash=canonical_hash(run.agent.config),
            source_hash=agent_spec.source_hash,
        ),
        graders=[
            ResolvedGrader(
                id=g.id,
                version=registry.graders[g.id].version,
                required=g.required,
                params=g.params,
            )
            for g in sc.graders
        ],
        grader_set_hash=compiled.grader_set_hash,
        seed=run.seed,
        mode=run.mode,
        logical_epoch=sc.task.logical_epoch,
        budgets=run.budgets,
        canonicalization_version=CANONICALIZATION_VERSION,
        fault_engine_version=FAULT_ENGINE_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        source=source,
        toolchain=toolchain,
        redaction=RedactionInfo(),
        replay_capability="full",  # mock runs capture every visible request/response
    )


def manifest_hash(manifest: Manifest) -> str:
    return canonical_hash(manifest.model_dump(mode="json"))
