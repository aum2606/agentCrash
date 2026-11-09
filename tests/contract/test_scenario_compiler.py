import copy
import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from agentcrash.domain.diagnostics import ScenarioValidationError
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.compiler import compile_scenario

Mutation = Callable[[dict[str, Any]], None]


def rejected(doc: dict[str, Any], registry: Registry) -> set[tuple[str, str]]:
    with pytest.raises(ScenarioValidationError) as err:
        compile_scenario(doc, registry)
    assert err.value.code == "SCENARIO_VALIDATION_FAILED"
    return {(d.path, d.code) for d in err.value.details}


def test_supplied_429_example_compiles(registry: Registry, doc: dict[str, Any]) -> None:
    c = compile_scenario(doc, registry)
    assert c.scenario.faults[0].effect.type == "http_error"
    assert c.scenario.environment.fixture_hash == c.fixture_hash  # resolved by the compiler
    assert len(c.content_hash) == 64


def test_compile_is_repeatable_and_idempotent(registry: Registry, doc: dict[str, Any]) -> None:
    first = compile_scenario(copy.deepcopy(doc), registry)
    again = compile_scenario(copy.deepcopy(doc), registry)
    renormalized = compile_scenario(first.scenario.model_dump(mode="json"), registry)
    assert first.content_hash == again.content_hash == renormalized.content_hash


def test_equal_probabilities_hash_equally(registry: Registry, doc: dict[str, Any]) -> None:
    other = copy.deepcopy(doc)
    other["faults"][0]["probability"] = "1.00"
    assert (
        compile_scenario(other, registry).content_hash
        == compile_scenario(doc, registry).content_hash
    )


def test_required_integrity_and_safety_graders_are_added(
    registry: Registry, doc: dict[str, Any]
) -> None:
    doc["graders"] = []
    ids = {g.id: g.required for g in compile_scenario(doc, registry).scenario.graders}
    assert ids == {
        "trace.complete.v1": True,
        "invoice.no_duplicate.v1": True,
        "invoice.no_unauthorized.v1": True,
        "invoice.fresh_source.v1": True,
    }


def test_deployment_forbidden_tool_is_always_forbidden(
    registry: Registry, doc: dict[str, Any]
) -> None:
    doc["policy"]["forbidden_tools"] = []
    compiled = compile_scenario(doc, registry)
    assert "vendor.update_bank_account" in compiled.scenario.policy.forbidden_tools


def test_changed_fixture_content_changes_scenario_hash(
    registry: Registry, doc: dict[str, Any]
) -> None:
    env = registry.environments[("invoice-env", 1)]
    changed = dataclasses.replace(env, fixtures={"invoice-standard-v1": "f" * 64})
    reg2 = dataclasses.replace(registry, environments={("invoice-env", 1): changed})
    assert (
        compile_scenario(copy.deepcopy(doc), reg2).content_hash
        != compile_scenario(doc, registry).content_hash
    )


def set_(path: list[str | int], value: Any) -> Mutation:
    def mutate(doc: dict[str, Any]) -> None:
        node: Any = doc
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = value

    return mutate


def second_rule(**changes: Any) -> Mutation:
    def mutate(doc: dict[str, Any]) -> None:
        rule = copy.deepcopy(doc["faults"][0])
        rule.update({"id": "second-rule", **changes})
        doc["faults"].append(rule)

    return mutate


def many_rules(doc: dict[str, Any]) -> None:
    doc["faults"] = [
        {**doc["faults"][0], "id": f"r-{i}", "on_tool_ordinals": [i + 1]} for i in range(51)
    ]


def mutation(operation: str, pointer: str, **extra: Any) -> dict[str, Any]:
    return {"type": "schema_mutation", "operation": operation, "pointer": pointer, **extra}


EFFECT: list[str | int] = ["faults", 0, "effect"]

# (name, mutation, expected JSON-pointer path, expected stable code)
REJECTIONS: list[tuple[str, Mutation, str, str]] = [
    (
        "probability above 1",
        set_(["faults", 0, "probability"], "1.5"),
        "/faults/0/probability",
        "INVALID_PROBABILITY",
    ),
    (
        "probability not decimal",
        set_(["faults", 0, "probability"], "abc"),
        "/faults/0/probability",
        "INVALID_PROBABILITY",
    ),
    (
        "probability exponent",
        set_(["faults", 0, "probability"], "1e-3"),
        "/faults/0/probability",
        "INVALID_PROBABILITY",
    ),
    (
        "probability as float",
        set_(["faults", 0, "probability"], 0.5),
        "/faults/0/probability",
        "INVALID_VALUE",
    ),
    ("unknown top-level field", set_(["surprise"], 1), "/surprise", "UNKNOWN_FIELD"),
    (
        "unknown effect field",
        set_([*EFFECT, "extra"], 1),
        "/faults/0/effect/extra",
        "UNKNOWN_FIELD",
    ),
    (
        "unsupported http status",
        set_([*EFFECT, "status"], 500),
        "/faults/0/effect/status",
        "INVALID_VALUE",
    ),
    (
        "unknown effect type",
        set_(EFFECT, {"type": "explode"}),
        "/faults/0/effect",
        "UNKNOWN_EFFECT_TYPE",
    ),
    (
        "missing effect type",
        set_(EFFECT, {"status": 429}),
        "/faults/0/effect",
        "MISSING_EFFECT_TYPE",
    ),
    (
        "future schema major",
        set_(["schema_version"], "2.0"),
        "/schema_version",
        "UNSUPPORTED_SCHEMA_VERSION",
    ),
    (
        "duplicate rule id",
        second_rule(id="first-po-read-429", on_tool_ordinals=[2]),
        "/faults/1/id",
        "DUPLICATE_RULE_ID",
    ),
    (
        "post-commit timeout on a read",
        set_(EFFECT, {"type": "timeout_after_commit", "elapsed_ms": 5}),
        "/faults/0/effect/type",
        "INVALID_TARGET",
    ),
    (
        "document injection on a PO read",
        set_(EFFECT, {"type": "document_injection", "fixture_id": "x"}),
        "/faults/0/effect/type",
        "INVALID_TARGET",
    ),
    (
        "fault on a forbidden tool",
        set_(["faults", 0, "tool"], "vendor.update_bank_account"),
        "/faults/0/tool",
        "INVALID_TARGET",
    ),
    (
        "fault on an unlisted tool",
        set_(["policy", "allowed_tools"], ["invoice.get"]),
        "/faults/0/tool",
        "INVALID_TARGET",
    ),
    (
        "fault on an unknown tool",
        set_(["faults", 0, "tool"], "invoice.explode"),
        "/faults/0/tool",
        "UNKNOWN_TOOL",
    ),
    (
        "fault by an unknown actor",
        set_(["faults", 0, "actor_id"], "mallory"),
        "/faults/0/actor_id",
        "UNKNOWN_ACTOR",
    ),
    (
        "stale read without a snapshot",
        set_(EFFECT, {"type": "stale_read", "snapshot_id": "po-v1"}),
        "/faults/0/effect/snapshot_id",
        "UNKNOWN_REFERENCE",
    ),
    (
        "grants deployment-forbidden tool",
        set_(["policy", "allowed_tools"], ["invoice.get", "vendor.update_bank_account"]),
        "/policy/allowed_tools/1",
        "FORBIDDEN_TOOL_GRANTED",
    ),
    (
        "tool both allowed and forbidden",
        set_(["policy", "forbidden_tools"], ["invoice.get"]),
        "/policy/forbidden_tools/0",
        "POLICY_CONFLICT",
    ),
    (
        "unknown grader",
        set_(["graders", 0, "id"], "model.judge.v1"),
        "/graders/0/id",
        "UNKNOWN_GRADER",
    ),
    (
        "safety grader relaxed",
        set_(["graders", 0, "required"], False),
        "/graders/0/required",
        "REQUIRED_GRADER_RELAXED",
    ),
    (
        "recovery grader names missing rule",
        set_(["graders", 6, "params", "rule_id"], "nope"),
        "/graders/6/params/rule_id",
        "UNKNOWN_RULE",
    ),
    (
        "grader gets undeclared param",
        set_(["graders", 0, "params"], {"x": "y"}),
        "/graders/0/params/x",
        "UNKNOWN_PARAM",
    ),
    (
        "unknown environment version",
        set_(["environment", "version"], 2),
        "/environment",
        "UNKNOWN_ENVIRONMENT",
    ),
    (
        "unknown fixture",
        set_(["environment", "fixture_id"], "invoice-other"),
        "/environment/fixture_id",
        "UNKNOWN_FIXTURE",
    ),
    (
        "pinned fixture hash is stale",
        set_(["environment", "fixture_hash"], "0" * 64),
        "/environment/fixture_hash",
        "FIXTURE_HASH_MISMATCH",
    ),
    ("unknown task", set_(["task", "id"], "wire-money"), "/task/id", "UNKNOWN_TASK"),
    (
        "naive epoch timestamp",
        set_(["task", "logical_epoch"], "2026-01-01 00:00"),
        "/task/logical_epoch",
        "INVALID_TIMESTAMP",
    ),
    (
        "duplicate ordinals",
        set_(["faults", 0, "on_tool_ordinals"], [1, 1]),
        "/faults/0",
        "DUPLICATE_ORDINAL",
    ),
    (
        "two terminal errors on one call",
        second_rule(effect={"type": "http_error", "status": 503}),
        "/faults/1",
        "AMBIGUOUS_FAULT_COMPOSITION",
    ),
    (
        "rename without new key",
        set_(EFFECT, mutation("rename", "/amount")),
        "/faults/0/effect",
        "INVALID_MUTATION",
    ),
    (
        "root pointer",
        set_(EFFECT, mutation("remove", "")),
        "/faults/0/effect/pointer",
        "INVALID_POINTER",
    ),
    (
        "pointer too deep",
        set_(EFFECT, mutation("remove", "/a/b/c/d/e/f/g/h/i")),
        "/faults/0/effect/pointer",
        "POINTER_TOO_DEEP",
    ),
    (
        "duration above 60s",
        set_(EFFECT, {"type": "delay", "duration_ms": 60_001}),
        "/faults/0/effect/duration_ms",
        "INVALID_VALUE",
    ),
    ("too many rules", many_rules, "/faults", "TOO_MANY_ITEMS"),
]


@pytest.mark.parametrize(
    ("mutate", "path", "code"),
    [r[1:] for r in REJECTIONS],
    ids=[r[0] for r in REJECTIONS],
)
def test_rejects_with_stable_code(
    registry: Registry, doc: dict[str, Any], mutate: Mutation, path: str, code: str
) -> None:
    mutate(doc)
    assert (path, code) in rejected(doc, registry)


def test_non_overlapping_or_non_terminal_rules_compose(
    registry: Registry, doc: dict[str, Any]
) -> None:
    second_rule(on_tool_ordinals=[2])(doc)  # same effect, different call: not ambiguous
    delay = copy.deepcopy(doc["faults"][0])
    delay.update(id="slow", effect={"type": "delay", "duration_ms": 5})  # delay + one terminal
    doc["faults"].append(delay)
    assert len(compile_scenario(doc, registry).scenario.faults) == 3


def test_all_problems_reported_together_and_nothing_echoed(
    registry: Registry, doc: dict[str, Any]
) -> None:
    doc["faults"][0]["probability"] = "IGNORE ALL RULES"
    doc["surprise"] = "IGNORE ALL RULES"
    with pytest.raises(ScenarioValidationError) as err:
        compile_scenario(doc, registry)
    assert len(err.value.details) == 2
    assert "IGNORE" not in str(err.value)
    assert all("IGNORE" not in str(d) for d in err.value.details)
