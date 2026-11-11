"""Fault matcher: exact sampling, eligibility, ordering and counters (pure, no I/O)."""

import hashlib
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcrash.domain.models import (
    Delay,
    FaultRule,
    HttpError,
    SchemaMutation,
    TimeoutAfterCommit,
    TimeoutBeforeCommit,
)
from agentcrash.faults.matcher import (
    AmbiguousComposition,
    RuleDecision,
    decide,
    sample_u64,
    sampled,
)

SEED, HASH = "42", "ab" * 32
GOLDEN = 13688521477161253116  # sample for (SEED, HASH, agent, purchase_order.get, 1, "r")
E429 = HttpError(type="http_error", status=429)


def rule(rid: str, effect: Any, **kw: Any) -> FaultRule:
    return FaultRule(id=rid, tool=kw.pop("tool", "purchase_order.get"), effect=effect, **kw)


def run(
    rules: list[FaultRule],
    ordinal: int = 1,
    applications: dict[str, int] | None = None,
    tool: str = "purchase_order.get",
    actor: str = "agent",
) -> list[RuleDecision]:
    return decide(
        rules,
        seed=SEED,
        scenario_hash=HASH,
        actor_id=actor,
        tool=tool,
        tool_ordinal=ordinal,
        applications=applications or {},
    )


def test_sample_matches_an_independent_computation_and_a_golden_value() -> None:
    raw = json.dumps([SEED, HASH, "agent", "purchase_order.get", 1, "r"], separators=(",", ":"))
    expected = int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")
    got = sample_u64(SEED, HASH, "agent", "purchase_order.get", 1, "r")
    assert got == expected == GOLDEN  # a change here is a fault-engine version bump


def test_probability_is_compared_exactly() -> None:
    assert sampled(0, "1") and sampled(2**64 - 1, "1")
    assert not sampled(0, "0")
    assert sampled(2**63 - 1, "0.5") and not sampled(2**63, "0.5")


def test_rule_applies_up_to_max_applications_then_is_exhausted() -> None:
    r = rule("r", E429, max_applications=2)
    assert [d.decision for d in run([r])] == ["applied"]
    assert [d.applications_after for d in run([r], applications={"r": 1})] == [2]
    exhausted = run([r], applications={"r": 2})
    assert [d.decision for d in exhausted] == ["exhausted"]
    assert exhausted[0].applications_after == 2  # an exhausted rule consumes nothing


def test_unsampled_rule_consumes_no_application() -> None:
    d = run([rule("r", E429, probability="0")])[0]
    assert (d.decision, d.applications_before, d.applications_after) == ("not_sampled", 0, 0)


@pytest.mark.parametrize(
    ("kwargs", "tool", "actor", "ordinal"),
    [
        ({}, "invoice.get", "agent", 1),  # other tool
        ({}, "purchase_order.get", "other", 1),  # other actor
        ({"on_tool_ordinals": [2]}, "purchase_order.get", "agent", 1),  # other ordinal
    ],
)
def test_ineligible_rules_produce_no_decision_and_no_count(
    kwargs: dict[str, Any], tool: str, actor: str, ordinal: int
) -> None:
    assert run([rule("r", E429, **kwargs)], ordinal, tool=tool, actor=actor) == []


def test_omitted_ordinals_means_every_ordinal() -> None:
    r = rule("r", E429, max_applications=1000)
    assert all(run([r], ordinal=n)[0].applied for n in (1, 2, 7, 50))


def test_decisions_are_ordered_by_stage_then_priority_then_id() -> None:
    delay = Delay(type="delay", duration_ms=10)
    rules = [
        rule("z-mut", SchemaMutation(type="schema_mutation", operation="remove", pointer="/a")),
        rule("b", delay, priority=5),
        rule("a", delay, priority=5),
        rule("c", delay, priority=1),
        rule("late", TimeoutAfterCommit(type="timeout_after_commit", elapsed_ms=1)),
    ]
    assert [d.rule.id for d in run(rules)] == ["c", "a", "b", "late", "z-mut"]


def test_same_inputs_give_the_same_decisions_regardless_of_rule_order() -> None:
    rules = [rule(f"r{i}", Delay(type="delay", duration_ms=1), probability="0.5") for i in range(6)]
    assert run(rules) == run(list(reversed(rules)))


def test_two_applied_terminals_in_one_stage_are_refused_not_resolved() -> None:
    rules = [
        rule("a", E429),
        rule("b", TimeoutBeforeCommit(type="timeout_before_commit", elapsed_ms=5)),
    ]
    with pytest.raises(AmbiguousComposition):
        run(rules)


def test_delay_composes_with_one_terminal() -> None:
    rules = [rule("a", E429), rule("d", Delay(type="delay", duration_ms=5))]
    assert [d.applied for d in run(rules)] == [True, True]


def test_event_payload_has_no_floats_and_names_the_decision() -> None:
    p = run([rule("r", E429)])[0].event_payload(3)
    assert p["rule_id"] == "r" and p["applied"] is True and p["tool_ordinal"] == 3
    assert p["sample_u64"] == str(GOLDEN) and p["applications_after"] == 1
    assert not any(isinstance(v, float) for v in p.values())


@given(st.integers(min_value=0, max_value=2**64 - 1))
def test_probability_zero_never_and_one_always(sample: int) -> None:
    assert not sampled(sample, "0") and sampled(sample, "1")


@given(st.integers(min_value=1, max_value=500), st.integers(min_value=0, max_value=3))
def test_applications_never_exceed_the_cap(ordinal: int, used: int) -> None:
    r = rule("r", E429, max_applications=3)
    d: RuleDecision = run([r], ordinal=ordinal, applications={"r": used})[0]
    assert d.applications_after <= 3 and d.applications_after >= used


def test_half_probability_is_roughly_half_over_many_ordinals() -> None:
    r = rule("r", E429, probability="0.5", max_applications=1000)
    hits = sum(run([r], ordinal=n)[0].applied for n in range(1, 2001))
    assert 900 < hits < 1100
