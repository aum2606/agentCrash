# ruff: noqa: F811
"""The comparison report built from real runs: the 10-case x 1-seed x 2-agent demo drained by a
worker, plus a run cancelled before it started and a run whose evaluator failed."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_api_flow import AUTH
from .test_execution import World
from .test_experiments_api import (  # noqa: F401  (fixtures)
    body,
    client,
    client_for,
    post,
    world,
)


def report(client: TestClient, experiment_id: str, query: str = "") -> Any:
    return client.get(f"/api/v1/experiments/{experiment_id}/report{query}", headers=AUTH)


def started(client: TestClient, **kw: Any) -> Any:
    return post(client, body(**kw)).json()


def test_an_unfinished_experiment_has_no_report_unless_partial_is_requested(
    world: World, client: TestClient
) -> None:
    exp = started(client)
    refused = report(client, exp["id"])
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EXPERIMENT_NOT_FINISHED"
    partial = report(client, exp["id"], "?partial=true")
    assert partial.status_code == 200
    doc = partial.json()
    assert (doc["expected_pairs"], doc["complete_pairs"], len(doc["missing_pairs"])) == (10, 0, 10)
    assert doc["gate"]["status"] == "incomplete"  # nothing is decided, so nothing is claimed
    assert doc["counts_by_agent"]["candidate"]["unfinished"] == 10
    assert doc["counts_by_agent"]["candidate"]["pass_rate"]["value"] == "0.0000"


def test_report_parameters_and_unknown_experiments_are_rejected(
    world: World, client: TestClient
) -> None:
    exp = started(client, scenarios=["invoice-429"])
    assert report(client, exp["id"], "?partial=maybe").status_code == 400
    assert report(client, exp["id"], "?other=1").status_code == 400
    assert report(client, "nope").status_code == 404
    assert client.get(f"/api/v1/experiments/{exp['id']}/report").status_code == 401


def test_the_demo_report_is_built_from_the_recorded_runs(world: World, client: TestClient) -> None:
    exp = started(client)
    world.drain(world.worker(), limit=60)
    got = report(client, exp["id"])
    assert got.status_code == 200
    doc = got.json()

    assert doc["gate"] == {"status": "pass", "reasons": []}  # the candidate passed every case
    assert (doc["expected_pairs"], doc["complete_pairs"], doc["missing_pairs"]) == (10, 10, [])
    cand, base = doc["counts_by_agent"]["candidate"], doc["counts_by_agent"]["baseline"]
    assert (cand["passed"], cand["failed"], cand["unfinished"]) == (10, 0, 0)
    assert base["passed"] + base["failed"] == 10 and base["failed"] > 0
    # Baseline failures never gate; they are listed with the reason the grader gave.
    assert doc["baseline_failures"] and all(
        p["baseline"]["verdict"] == "fail" and p["baseline"]["primary_reason"]
        for p in doc["baseline_failures"]
    )
    assert doc["paired_completion_delta"][0]["scenario_id"] is None
    assert doc["paired_completion_delta"][0]["pairs"] == 10

    # Fault exposure comes from fault.decided events: the healthy case has none, 429 has some.
    exposure = {(x["scenario_id"], x["role"]): x for x in doc["fault_exposure_by_scenario"]}
    assert exposure[("invoice-healthy", "candidate")]["exposed_runs"] == 0
    assert exposure[("invoice-429", "candidate")]["exposed_runs"] == 1
    assert exposure[("invoice-429", "candidate")]["recovery_rate"] == {
        "n": 1,
        "d": 1,
        "value": "1.0000",
    }
    # Usage and latency come from the recorded trace.
    assert doc["usage_summary"]["candidate"]["tool_calls"]["count"] == 10
    assert doc["usage_summary"]["candidate"]["steps"]["min"] >= 1
    assert doc["latency_summary"]["baseline"]["logical_ms"]["warning"] == "SMALL_SAMPLE"
    assert doc["latency_summary"]["candidate"]["wall_ms"]["count"] == 10
    # The injected document makes the naive agent attempt something it may not do.
    assert doc["safety_counts"]["baseline"]["attempted_unsafe"] >= 1
    assert doc["safety_counts"]["candidate"] == {
        "attempted_unsafe": 0,
        "committed_unsafe": 0,
        "duplicate_write": 0,
        "stale_source": 0,
        "trace_incomplete": 0,
    }
    assert len(doc["manifest_hashes"]) == 20
    # Reading the report changes nothing, so a second read is identical.
    assert report(client, exp["id"]).json() == doc


def test_a_cancelled_run_leaves_the_gate_incomplete_and_is_named(
    world: World, client: TestClient
) -> None:
    exp = started(client, scenarios=["invoice-healthy", "invoice-429"])
    victim = next(
        s["run_id"]
        for s in exp["slots"]
        if s["role"] == "candidate" and s["scenario_id"] == "invoice-429"
    )
    cancel = client.post(f"/api/v1/runs/{victim}/cancel", json={"reason": "x"}, headers=AUTH)
    assert cancel.status_code in (200, 202)
    world.drain(world.worker(), limit=20)
    doc = report(client, exp["id"]).json()

    assert doc["gate"]["status"] == "incomplete"
    assert [r["code"] for r in doc["gate"]["reasons"]] == ["PAIR_INCOMPLETE"]
    assert "candidate:CANCELLED" in doc["gate"]["reasons"][0]["detail"]
    assert doc["counts_by_agent"]["candidate"]["cancelled"] == 1
    assert doc["counts_by_agent"]["baseline"]["cancelled"] == 0
    assert [m["scenario_id"] for m in doc["missing_pairs"]] == ["invoice-429"]
    assert doc["missing_pairs"][0]["missing"] == ["candidate"]
    assert (doc["complete_pairs"], doc["expected_pairs"]) == (1, 2)


def test_a_run_whose_evaluator_failed_is_never_counted_as_a_pass(
    world: World, client: TestClient
) -> None:
    exp = started(client, scenarios=["invoice-healthy", "invoice-429"])
    world.drain(world.worker(), limit=20)
    victim = next(
        s["run_id"]
        for s in exp["slots"]
        if s["role"] == "candidate" and s["scenario_id"] == "invoice-healthy"
    )
    with world.engine.begin() as conn:  # what the worker records when grading itself fails
        conn.execute(
            text(
                "UPDATE runs SET status = 'error', verdict = NULL, error_code = 'EVALUATOR_ERROR' "
                "WHERE id = :r"
            ),
            {"r": victim},
        )
    doc = report(client, exp["id"]).json()
    assert doc["gate"]["status"] == "incomplete"
    assert doc["gate"]["reasons"][0]["code"] == "PAIR_INCOMPLETE"
    assert "RUN_ERROR:EVALUATOR_ERROR" in doc["gate"]["reasons"][0]["detail"]
    assert doc["counts_by_agent"]["candidate"]["errored"] == 1
    assert doc["counts_by_agent"]["candidate"]["passed"] == 1
    assert client.get(f"/api/v1/experiments/{exp['id']}", headers=AUTH).json()["status"] == "error"


def test_a_failing_candidate_fails_the_gate_even_while_other_runs_are_unfinished(
    world: World, client: TestClient
) -> None:
    """The baseline agent stands in as a weak candidate: its failures must fail the gate."""
    weak = {"id": "invoice-naive", "version": "1.0.0"}
    exp = post(client, body(scenarios=["invoice-document-injection"], candidate_agent=weak)).json()
    baseline = next(s["run_id"] for s in exp["slots"] if s["role"] == "baseline")
    client.post(f"/api/v1/runs/{baseline}/cancel", headers=AUTH)  # the pair stays incomplete
    world.drain(world.worker(), limit=10)
    got = report(client, exp["id"]).json()
    assert got["complete_pairs"] == 0
    assert got["gate"]["status"] == "fail"  # a decided candidate failure outranks "incomplete"
    codes = {r["code"] for r in got["gate"]["reasons"]}
    assert {"CANDIDATE_REQUIRED_FAILURE", "CANDIDATE_UNSAFE_ATTEMPT", "PAIR_INCOMPLETE"} <= codes
    assert got["safety_counts"]["candidate"]["attempted_unsafe"] == 1


def test_timing_never_enters_the_gate(world: World, client: TestClient) -> None:
    exp = started(client, scenarios=["invoice-429"])
    world.drain(world.worker(), limit=10)
    first = report(client, exp["id"]).json()
    with world.engine.begin() as conn:  # make one attempt look absurdly slow
        conn.execute(text("UPDATE run_attempts SET closed_at = created_at + interval '1 day'"))
    slow = report(client, exp["id"]).json()
    assert slow["gate"] == first["gate"]
    assert slow["latency_summary"]["candidate"]["wall_ms"]["max"] == 86_400_000


@pytest.mark.parametrize("seeds", [["1", "2"]])
def test_several_seeds_make_several_pairs_per_scenario(
    world: World, client: TestClient, seeds: list[str]
) -> None:
    exp = started(client, scenarios=["invoice-429"], seeds=seeds)
    world.drain(world.worker(), limit=20)
    doc = report(client, exp["id"]).json()
    assert (doc["expected_pairs"], doc["complete_pairs"]) == (2, 2)
    assert sorted(p["seed"] for p in doc["pairs"]) == seeds


def _per_run(world: World, experiment_id: str, role: str, event: str, applied: bool) -> list[int]:
    with world.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT count(e.seq) FROM experiment_slots s LEFT JOIN events e "
                "ON e.run_id = s.run_id AND e.type = :t "
                "AND (NOT :applied OR (e.payload->>'applied')::boolean) "
                "WHERE s.experiment_id = :x AND s.agent_role = :r GROUP BY s.run_id"
            ),
            {"x": experiment_id, "r": role, "t": event, "applied": applied},
        )
        return sorted(int(n) for (n,) in rows)


def test_counts_in_the_report_match_the_recorded_events(world: World, client: TestClient) -> None:
    exp = started(client)
    world.drain(world.worker(), limit=60)
    doc = report(client, exp["id"]).json()
    for role in ("baseline", "candidate"):
        steps = _per_run(world, exp["id"], role, "agent.step", False)
        calls = _per_run(world, exp["id"], role, "tool.requested", False)
        usage = doc["usage_summary"][role]
        assert (usage["steps"]["min"], usage["steps"]["max"]) == (steps[0], steps[-1])
        assert (usage["tool_calls"]["min"], usage["tool_calls"]["max"]) == (calls[0], calls[-1])
        applied = sum(_per_run(world, exp["id"], role, "fault.decided", True))
        assert applied == sum(
            x["applications"] for x in doc["fault_exposure_by_scenario"] if x["role"] == role
        )


def test_a_fault_rule_that_was_considered_but_not_applied_is_not_exposure(
    world: World, client: TestClient
) -> None:
    """Every eligible rule leaves a ``fault.decided`` event; only applied ones are exposure."""
    from agentcrash.bootstrap import default_registry
    from agentcrash.scenarios.compiler import compile_scenario
    from agentcrash.scenarios.loader import load_scenario_document

    from .helpers import ROOT

    raw = (ROOT / "scenarios/invoice/v1/invoice-429.yaml").read_bytes().decode()
    never = raw.replace("id: invoice-429", "id: invoice-429-never").replace(
        'probability: "1"', 'probability: "0"'
    )
    world.store.put_scenario(
        compile_scenario(load_scenario_document(never.encode()), default_registry(ROOT))
    )
    exp = started(client, scenarios=["invoice-429-never"])
    world.drain(world.worker(), limit=10)
    decided = _per_run(world, exp["id"], "candidate", "fault.decided", False)
    assert decided == [1]  # the rule was considered ...
    row = report(client, exp["id"]).json()["fault_exposure_by_scenario"][1]
    assert (row["scenario_id"], row["role"]) == ("invoice-429-never", "candidate")
    assert (row["exposed_runs"], row["applications"]) == (0, 0)  # ... and never applied
