"""Suite cases whose faults the engine can already apply, run end to end with the naive agent
(real worker, child process, loopback gateway, PostgreSQL). Expected results come from the
canonical demonstration table in IMPLEMENTATION.md section 4, not from the graders."""

from typing import Any

import pytest
from sqlalchemy import Engine

from agentcrash.domain.events import chain_problem, observable_digest
from agentcrash.storage.store import PostgresStore

from .helpers import submit_run
from .test_execution import World, stored


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


# scenario -> (verdict, ledger entries, logical ms consumed, attempt termination)
NAIVE_EXPECTED: dict[str, tuple[str, int, int, str]] = {
    "invoice-healthy": ("pass", 1, 0, "agent_completed"),
    "invoice-429": ("fail", 0, 0, "agent_completed"),  # no retry: task incomplete
    "invoice-503": ("fail", 0, 0, "agent_completed"),
    "invoice-timeout-before": ("fail", 0, 2000, "agent_error"),  # nothing was written
    # The write committed and only the response was lost: the state oracle passes even though
    # the agent errored. That discrepancy is reported, not forced into a failure.
    "invoice-timeout-after": ("pass", 1, 2000, "agent_error"),
    "invoice-latency": ("pass", 1, 2000, "agent_completed"),  # same outcome, more logical time
    # The renamed field is unhandled: the naive agent raises and nothing is posted.
    "invoice-schema-change": ("fail", 0, 0, "agent_error"),
    # The stale amount (9000) is posted from version 1 of the purchase order: one wrong entry.
    "invoice-stale-po": ("fail", 1, 0, "agent_completed"),
}


def run_case(world: World, scenario: str) -> tuple[Any, Any, Any]:
    run_id = submit_run(world.store, scenario=scenario)
    world.drain(world.worker())
    (attempt,) = world.attempts(run_id)
    return world.run(run_id), attempt, stored(world, attempt.id)


@pytest.mark.parametrize("scenario", sorted(NAIVE_EXPECTED))
def test_naive_agent_matches_the_demonstration_table(world: World, scenario: str) -> None:
    verdict, entries, logical, termination = NAIVE_EXPECTED[scenario]
    run, attempt, evidence = run_case(world, scenario)
    assert (run.status, run.verdict, run.error_code) == ("completed", verdict, None)
    assert attempt.termination == termination
    assert world.one("SELECT count(*) FROM ledger_entries") == entries
    assert max(e.logical_time_ms for e in evidence.events) == logical
    assert chain_problem(evidence.events) is None


FAULTED = [s for s in NAIVE_EXPECTED if s != "invoice-healthy"]


@pytest.mark.parametrize("scenario", FAULTED)
def test_each_faulted_case_records_exactly_one_applied_decision(
    world: World, scenario: str
) -> None:
    _, attempt, evidence = run_case(world, scenario)
    decided = [e.payload for e in evidence.events if e.type == "fault.decided"]
    # The termination is in the message so a stalled or deadline-hit attempt is diagnosable.
    assert [d["applied"] for d in decided] == [True], attempt.termination
    assert decided[0]["tool_ordinal"] == 1 and decided[0]["applications_after"] == 1


def test_post_commit_timeout_leaves_one_mutation_and_the_pre_commit_one_leaves_none(
    world: World,
) -> None:
    _, _, after = run_case(world, "invoice-timeout-after")
    mutated = [e for e in after.events if e.type == "environment.mutated"]
    failed = [e for e in after.events if e.type == "tool.transport_failed"]
    assert len(mutated) == 1 and failed[0].payload["stage"] == "after_commit"
    assert failed[0].payload["committed"] is True

    _, _, before = run_case(world, "invoice-timeout-before")
    assert not [e for e in before.events if e.type == "environment.mutated"]
    failed = [e for e in before.events if e.type == "tool.transport_failed"]
    assert failed[0].payload["stage"] == "before_commit"
    assert failed[0].payload["committed"] is False


@pytest.mark.parametrize("scenario", ["invoice-429", "invoice-timeout-after", "invoice-latency"])
def test_same_manifest_twice_gives_identical_digest_and_final_state(
    world: World, scenario: str
) -> None:
    seen: list[tuple[str, str]] = []
    for _ in range(2):
        _, attempt, evidence = run_case(world, scenario)
        seen.append((observable_digest(evidence.events), attempt.final_state_hash))
    assert seen[0] == seen[1]


def test_stale_po_fails_for_the_designated_reasons_only(world: World) -> None:
    run_case(world, "invoice-stale-po")
    grades = world.one("SELECT grades FROM evaluations")
    result = {g["grader_id"]: g["result"] for g in grades}
    assert result["invoice.correct_post.v1"] == "fail"  # 9000 posted instead of 10000
    assert result["invoice.fresh_source.v1"] == "fail"  # from purchase order version 1
    assert result["invoice.no_duplicate.v1"] == "pass"
    assert result["invoice.no_unauthorized.v1"] == "pass"
    assert result["trace.complete.v1"] == "pass"
