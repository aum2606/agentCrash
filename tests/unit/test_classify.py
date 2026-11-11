"""Failure classification over hand-built evidence: all findings kept, one primary chosen by
priority, nothing invented for a passing run, incomplete evidence outranks everything."""

from typing import Any
from uuid import uuid4

import pytest

from agentcrash.domain.evidence import REASON_PRIORITY, Evidence, Grade
from agentcrash.domain.models import Budgets
from agentcrash.evaluation.classify import classify
from unit.test_graders import Chain


def grade(grader_id: str, result: str, reason: str) -> Grade:
    return Grade(
        grader_id=grader_id,
        grader_version=1,
        grader_hash="0" * 64,
        required=True,
        result=result,  # type: ignore[arg-type]
        reason_code=reason,
    )


def evidence(chain: Chain) -> Evidence:
    return Evidence(
        attempt_id=uuid4(),
        termination="agent_completed",
        closed=True,
        events=chain.events,
        tool_calls=[],
        initial_state=None,
        final_state=None,
        final_state_hash=None,
        chain_head=chain.head,
        budgets=Budgets(),
    )


def completed(chain: Chain, tool: str, status: int, **extra: Any) -> None:
    chain.add("tool.completed", {"tool": tool, "status": status, **extra})


def test_a_passing_run_has_no_primary_reason_and_no_invented_findings() -> None:
    primary, findings = classify(
        evidence(Chain()), [grade("trace.complete.v1", "pass", "OK")], "pass"
    )
    assert (primary, findings) == (None, [])


def test_primary_reason_follows_priority_and_every_finding_is_kept() -> None:
    grades = [
        grade("budget.within_limits.v1", "fail", "BUDGET_EXCEEDED"),
        grade("invoice.correct_post.v1", "fail", "TASK_INCOMPLETE"),
        grade("invoice.no_unauthorized.v1", "fail", "UNAUTHORIZED_ATTEMPT"),
        grade("recovery.observed.v1", "unknown", "NO_FAULT_EXPOSURE"),  # not a taxonomy code
    ]
    primary, findings = classify(evidence(Chain()), grades, "fail")
    assert primary == "UNAUTHORIZED_ATTEMPT"
    assert [f.code for f in findings] == [
        "UNAUTHORIZED_ATTEMPT",
        "TASK_INCOMPLETE",
        "BUDGET_EXCEEDED",
    ]


def test_incomplete_evidence_outranks_everything() -> None:
    grades = [
        grade("invoice.no_unauthorized.v1", "fail", "UNAUTHORIZED_COMMIT"),
        grade("trace.complete.v1", "fail", "TRACE_INCOMPLETE"),
    ]
    assert classify(evidence(Chain()), grades, "inconclusive")[0] == "TRACE_INCOMPLETE"


def test_a_specific_task_cause_outranks_the_generic_task_incomplete() -> None:
    grades = [
        grade("invoice.correct_post.v1", "fail", "TASK_INCOMPLETE"),
        grade("recovery.observed.v1", "fail", "RETRY_EXHAUSTED"),
    ]
    assert classify(evidence(Chain()), grades, "fail")[0] == "RETRY_EXHAUSTED"


def test_a_loop_flag_in_the_trace_is_a_finding_even_on_a_passing_run() -> None:
    chain = Chain()
    completed(chain, "invoice.get", 503, loop_suspected={"consecutive_identical_reads": 3})
    primary, findings = classify(evidence(chain), [], "pass")
    assert primary is None and [f.code for f in findings] == ["LOOP_SUSPECTED"]
    assert classify(evidence(chain), [], "fail")[0] == "LOOP_SUSPECTED"


MUTATION = {"rule_id": "r", "effect": "schema_mutation", "applied": True}


def test_an_unrecovered_schema_mutation_is_schema_unhandled() -> None:
    chain = Chain()
    completed(chain, "purchase_order.get", 200, mutation=MUTATION)
    primary, findings = classify(
        evidence(chain), [grade("invoice.correct_post.v1", "fail", "TASK_INCOMPLETE")], "fail"
    )
    assert primary == "SCHEMA_UNHANDLED" and findings[0].detail == "purchase_order.get"


def test_a_later_clean_read_means_the_schema_change_was_handled() -> None:
    chain = Chain()
    completed(chain, "purchase_order.get", 200, mutation=MUTATION)
    completed(chain, "purchase_order.get", 200)
    codes = [f.code for f in classify(evidence(chain), [], "fail")[1]]
    assert "SCHEMA_UNHANDLED" not in codes


def test_a_mutation_that_did_not_apply_is_not_a_finding() -> None:
    chain = Chain()
    completed(chain, "purchase_order.get", 404, mutation={**MUTATION, "applied": False})
    assert classify(evidence(chain), [], "fail")[1] == []


@pytest.mark.parametrize("code", REASON_PRIORITY)
def test_every_priority_code_is_classifiable(code: str) -> None:
    primary, findings = classify(evidence(Chain()), [grade("g", "fail", code)], "fail")
    assert primary == code and [f.code for f in findings] == [code]
