import copy
import dataclasses
from typing import Any

import pytest

from agentcrash.domain.diagnostics import RunValidationError
from agentcrash.domain.models import SourceInfo, ToolchainInfo
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.manifest import build_manifest, manifest_hash, validate_run_create

RUN: dict[str, Any] = {
    "schema_version": "1.0",
    "scenario": {"id": "invoice-429", "version": 1},
    "agent": {"id": "invoice-resilient", "version": "1.0.0", "config": {}},
    "seed": "42",
    "mode": "mock",
    "budgets": {"wall_ms": 120000, "logical_ms": 60000, "tool_calls": 50, "steps": 20},
    "labels": {"purpose": "demo"},
}
SOURCE = SourceInfo(commit=None, dirty=True)
TOOLCHAIN = ToolchainInfo(python="3.12.13", lock_hash=None)


def run(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(RUN)
    out.update(changes)
    return out


def rejected(document: dict[str, Any], registry: Registry) -> set[tuple[str, str]]:
    with pytest.raises(RunValidationError) as err:
        validate_run_create(document, registry)
    assert err.value.code == "RUN_VALIDATION_FAILED"
    return {(d.path, d.code) for d in err.value.details}


def test_contract_example_run_is_valid(registry: Registry) -> None:
    assert validate_run_create(RUN, registry).seed == "42"


def test_budgets_default_to_caps(registry: Registry) -> None:
    r = validate_run_create({k: v for k, v in RUN.items() if k != "budgets"}, registry)
    b = r.budgets
    assert (b.wall_ms, b.logical_ms, b.tool_calls, b.steps) == (120000, 60000, 50, 20)


UNKNOWN_NAIVE = {"id": "invoice-naive", "version": "1.0.0", "config": {"model": "x"}}


@pytest.mark.parametrize(
    ("changes", "path", "code"),
    [
        ({"agent": {"id": "invoice-evil", "version": "1.0.0"}}, "/agent/id", "UNKNOWN_ADAPTER"),
        ({"agent": {"id": "invoice-naive", "version": "9.9.9"}}, "/agent/id", "UNKNOWN_ADAPTER"),
        ({"agent": UNKNOWN_NAIVE}, "/agent/config/model", "UNKNOWN_FIELD"),
        ({"seed": "-1"}, "/seed", "INVALID_SEED"),
        ({"seed": "007"}, "/seed", "INVALID_SEED"),
        ({"seed": "18446744073709551616"}, "/seed", "INVALID_SEED"),
        ({"seed": 42}, "/seed", "INVALID_VALUE"),
        ({"mode": "live"}, "/mode", "UNSUPPORTED_MODE"),
        ({"budgets": {"tool_calls": 51}}, "/budgets/tool_calls", "BUDGET_EXCEEDS_CAP"),
        ({"budgets": {"wall_ms": 120001}}, "/budgets/wall_ms", "BUDGET_EXCEEDS_CAP"),
        ({"labels": {f"k{i}": "v" for i in range(11)}}, "/labels", "TOO_MANY_LABELS"),
        ({"labels": {"k": "v" * 129}}, "/labels", "LABEL_TOO_LONG"),
        ({"api_key": "sk-123"}, "/api_key", "UNKNOWN_FIELD"),
    ],
)
def test_run_create_rejections(
    registry: Registry, changes: dict[str, Any], path: str, code: str
) -> None:
    assert (path, code) in rejected(run(**changes), registry)


def test_lower_budgets_and_max_seed_are_accepted(registry: Registry) -> None:
    r = validate_run_create(run(seed="18446744073709551615", budgets={"tool_calls": 5}), registry)
    assert r.budgets.tool_calls == 5


def manifest_digest(registry: Registry, doc: dict[str, Any], **run_changes: Any) -> str:
    compiled = compile_scenario(copy.deepcopy(doc), registry)
    request = validate_run_create(run(**run_changes), registry)
    manifest = build_manifest(compiled, request, registry, source=SOURCE, toolchain=TOOLCHAIN)
    return manifest_hash(manifest)


def test_manifest_hash_is_stable_and_input_sensitive(
    registry: Registry, doc: dict[str, Any]
) -> None:
    base = manifest_digest(registry, doc)
    assert base == manifest_digest(registry, doc)
    assert base != manifest_digest(registry, doc, seed="43")
    assert base != manifest_digest(registry, doc, budgets={"tool_calls": 5})
    naive = {"id": "invoice-naive", "version": "1.0.0", "config": {}}
    assert base != manifest_digest(registry, doc, agent=naive)
    doc["faults"][0]["effect"]["status"] = 503
    assert base != manifest_digest(registry, doc)


def test_manifest_records_provenance_and_materialized_inputs(
    registry: Registry, doc: dict[str, Any]
) -> None:
    compiled = compile_scenario(doc, registry)
    request = validate_run_create(RUN, registry)
    m = build_manifest(compiled, request, registry, source=SOURCE, toolchain=TOOLCHAIN)
    assert m.scenario == compiled.scenario  # materialized content, not a name
    assert m.fixture.hash == compiled.fixture_hash
    assert m.source.dirty is True
    assert m.replay_capability == "full"
    assert m.canonicalization_version == "acl-c14n-v1"


def test_changed_fixture_content_changes_manifest_hash(
    registry: Registry, doc: dict[str, Any]
) -> None:
    env = registry.environments[("invoice-env", 1)]
    changed = dataclasses.replace(env, fixtures={"invoice-standard-v1": "e" * 64})
    reg2 = dataclasses.replace(registry, environments={("invoice-env", 1): changed})
    assert manifest_digest(registry, doc) != manifest_digest(reg2, doc)
