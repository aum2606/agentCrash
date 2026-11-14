# ruff: noqa: F811
"""`agentcrash experiments` against the real API, PostgreSQL and a worker: the shipped suite passes
with exit 0, and every way the gate can be wrong or unreadable gives a nonzero exit code."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from agentcrash.cli import experiments as cli
from agentcrash.cli.main import app

from .helpers import ROOT
from .test_api_flow import AUTH
from .test_execution import World
from .test_experiments_api import client_for, world  # noqa: F401

runner = CliRunner()
SMALL = """schema_version: "1.0"
name: small
scenario_refs:
  - {{id: invoice-healthy, version: 1}}
  - {{id: invoice-429, version: 1}}
baseline_agent: {{id: invoice-naive, version: "1.0.0"}}
candidate_agent: {{id: {candidate}, version: "1.0.0"}}
seeds: ["42"]
trials_per_seed: 1
mode: mock
gate_policy_id: mock-release.v1
"""


@pytest.fixture(autouse=True)
def wired(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def make(url: str) -> Any:
        client = client_for(world)
        client.headers.update(AUTH)
        return client

    monkeypatch.setattr(cli, "make_client", make)
    worker = world.worker()
    monkeypatch.setattr(cli, "pause", lambda seconds: world.drain(worker, limit=60))


def cli_run(*args: str) -> Any:
    return runner.invoke(app, ["experiments", *args])


def suite_file(tmp_path: Path, candidate: str = "invoice-resilient") -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(SMALL.format(candidate=candidate))
    return path


def create(tmp_path: Path, candidate: str = "invoice-resilient") -> str:
    result = cli_run("run", "--suite", str(suite_file(tmp_path, candidate)))
    assert result.exit_code == 0, result.output
    return str(result.stdout.strip())


def test_the_shipped_suite_runs_and_passes_with_exit_zero(world: World) -> None:
    suite = ROOT / "scenarios" / "invoice" / "v1" / "suite.yaml"
    result = cli_run("run", "--suite", str(suite), "--wait", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["gate"] == {"status": "pass", "reasons": []}
    assert (report["expected_pairs"], report["complete_pairs"]) == (10, 10)
    assert world.one("SELECT count(*) FROM runs") == 20
    assert report["baseline_failures"]  # the weak baseline's failures are visible
    # The same experiment read again through `gate` gives the same answer and exit code.
    again = cli_run("gate", report["experiment_id"], "--json")
    assert again.exit_code == 0 and json.loads(again.stdout) == report


def test_a_fabricated_success_message_with_the_wrong_ledger_fails_the_gate(
    world: World, tmp_path: Path
) -> None:
    """The naive agent posts against a stale purchase order and then reports "completed"."""
    path = tmp_path / "stale.yaml"
    path.write_text(
        SMALL.format(candidate="invoice-naive").replace("invoice-429", "invoice-stale-po")
    )
    result = cli_run("run", "--suite", str(path), "--wait", "--json")
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["gate"]["status"] == "fail"
    claimed = world.one(
        "SELECT e.payload->>'status' FROM events e JOIN experiment_slots s ON s.run_id = e.run_id "
        "JOIN runs r ON r.id = s.run_id WHERE s.agent_role = 'candidate' "
        "AND r.scenario_id = 'invoice-stale-po' AND e.type = 'agent.finished'"
    )
    assert claimed == "completed"  # the agent said it succeeded ...
    stale = next(p for p in report["pairs"] if p["scenario_id"] == "invoice-stale-po")
    assert stale["candidate"]["verdict"] == "fail"  # ... and the ledger says otherwise
    assert stale["candidate"]["primary_reason"] in {"STALE_SOURCE", "WRONG_STATE"}
    assert "CANDIDATE_REQUIRED_FAILURE" in {r["code"] for r in report["gate"]["reasons"]}


def test_a_lost_job_means_no_gate_and_exit_two(world: World, tmp_path: Path) -> None:
    experiment = create(tmp_path)
    lost = world.one(
        "SELECT run_id FROM experiment_slots WHERE agent_role = 'candidate' "
        "AND scenario_id = 'invoice-429'"
    )
    with world.engine.begin() as conn:  # the job vanishes: the run is never executed
        conn.execute(text("UPDATE jobs SET status = 'done' WHERE run_id = :r"), {"r": lost})
    world.drain(world.worker(), limit=60)
    result = cli_run("gate", experiment)
    assert result.exit_code == 2
    assert "EXPERIMENT_NOT_FINISHED" in result.output
    waited = cli_run("wait", experiment, "--timeout", "0")
    assert waited.exit_code == 2 and "still running" in waited.output


def test_mismatched_grader_sets_and_evaluator_failures_cannot_pass(
    world: World, tmp_path: Path
) -> None:
    experiment = create(tmp_path)
    world.drain(world.worker(), limit=60)
    assert cli_run("gate", experiment).exit_code == 0

    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET manifest = jsonb_set(manifest, '{grader_set_hash}', :h) "
                "WHERE id = (SELECT run_id FROM experiment_slots WHERE agent_role = 'candidate' "
                "AND scenario_id = 'invoice-429')"
            ),
            {"h": json.dumps("0" * 64)},
        )
    mismatched = cli_run("gate", experiment)
    assert mismatched.exit_code == 2 and "GRADER_SET_MISMATCH" in mismatched.output

    with world.engine.begin() as conn:  # undo, then simulate an evaluator failure instead
        conn.execute(
            text(
                "UPDATE runs SET manifest = jsonb_set(manifest, '{grader_set_hash}', "
                "(SELECT manifest->'grader_set_hash' FROM runs WHERE scenario_id = 'invoice-429' "
                "AND agent_id = 'invoice-naive')) WHERE id = (SELECT run_id FROM experiment_slots "
                "WHERE agent_role = 'candidate' AND scenario_id = 'invoice-429')"
            )
        )
    assert cli_run("gate", experiment).exit_code == 0
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET status = 'error', verdict = NULL, error_code = 'EVALUATOR_ERROR' "
                "WHERE id = (SELECT run_id FROM experiment_slots WHERE agent_role = 'candidate' "
                "AND scenario_id = 'invoice-healthy')"
            )
        )
    failed = cli_run("gate", experiment)
    assert failed.exit_code == 2 and "EVALUATOR_ERROR" in failed.output


def test_a_cancelled_run_exits_two_not_zero(world: World, tmp_path: Path) -> None:
    experiment = create(tmp_path)
    victim = world.one(
        "SELECT run_id FROM experiment_slots WHERE agent_role = 'candidate' "
        "AND scenario_id = 'invoice-healthy'"
    )
    client = client_for(world)
    assert client.post(f"/api/v1/runs/{victim}/cancel", headers=AUTH).status_code in (200, 202)
    world.drain(world.worker(), limit=60)
    result = cli_run("gate", experiment)
    assert result.exit_code == 2 and "CANCELLED" in result.output


BASELINE = ROOT / "baselines" / "invoice-mock-regression.json"


def test_two_runs_of_the_suite_give_the_same_baseline_and_it_matches_the_reviewed_file(
    world: World, tmp_path: Path
) -> None:
    suite = str(ROOT / "scenarios" / "invoice" / "v1" / "suite.yaml")
    views = []
    for n in range(2):
        experiment = cli_run("run", "--suite", suite).stdout.strip()
        assert cli_run("wait", experiment).exit_code == 0
        out = tmp_path / f"view{n}.json"
        assert cli_run("baseline", experiment, "--output", str(out)).exit_code == 0
        views.append(out.read_bytes())
        # Gating against the committed baseline passes only while nothing has drifted.
        assert cli_run("gate", experiment, "--baseline", str(BASELINE)).exit_code == 0
    assert views[0] == views[1]  # fresh run ids, same file
    assert views[0] == BASELINE.read_bytes(), (
        "results differ from the reviewed baseline; update baselines/ only in a reviewed change"
    )
    text = views[0].decode()
    for machine_specific in ("run_id", "experiment_id", "manifest_hashes", "wall_ms"):
        assert machine_specific not in text


def test_drift_from_the_reviewed_baseline_fails_a_passing_gate(
    world: World, tmp_path: Path
) -> None:
    experiment = create(tmp_path)
    world.drain(world.worker(), limit=60)
    # A small suite can never equal the full-suite baseline: pairs are missing from it.
    drifted = cli_run("gate", experiment, "--baseline", str(BASELINE))
    assert drifted.exit_code == 1 and "BASELINE_DRIFT" in drifted.output
    assert cli_run("gate", experiment).exit_code == 0  # same experiment, no baseline: pass
    missing = cli_run("gate", experiment, "--baseline", str(tmp_path / "nope.json"))
    assert missing.exit_code == 2


def test_only_a_passing_experiment_can_be_written_as_a_baseline(
    world: World, tmp_path: Path
) -> None:
    experiment = create(tmp_path)  # nothing has run: the report is not even readable yet
    out = tmp_path / "b.json"
    assert cli_run("baseline", experiment, "--output", str(out)).exit_code == 2
    assert not out.exists()


def test_an_incomplete_but_readable_report_is_not_accepted_as_a_baseline(
    world: World, tmp_path: Path
) -> None:
    experiment = create(tmp_path)
    victim = world.one("SELECT run_id FROM experiment_slots WHERE agent_role = 'candidate' LIMIT 1")
    client = client_for(world)
    client.post(f"/api/v1/runs/{victim}/cancel", headers=AUTH)
    world.drain(world.worker(), limit=60)
    out = tmp_path / "b.json"
    result = cli_run("baseline", experiment, "--output", str(out))
    assert result.exit_code == 2 and "only a passing run" in result.output and not out.exists()
