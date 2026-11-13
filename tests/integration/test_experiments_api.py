"""Experiments through the HTTP API, PostgreSQL and the worker: atomic matrix creation under a
whole-batch queue cap, idempotency, validation without side effects, immutable slots, and the
10-case x 1-seed x 2-agent demo running end to end."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from agentcrash.api.app import create_app
from agentcrash.bootstrap import default_registry
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.storage.store import PostgresStore

from .helpers import ROOT
from .test_api_flow import AUTH, make_services
from .test_execution import World

SUITE = [
    "invoice-healthy",
    "invoice-429",
    "invoice-503",
    "invoice-timeout-before",
    "invoice-timeout-after",
    "invoice-latency",
    "invoice-schema-change",
    "invoice-stale-po",
    "invoice-document-injection",
    "invoice-cas-conflict",
]
BASELINE = {"id": "invoice-naive", "version": "1.0.0"}
CANDIDATE = {"id": "invoice-resilient", "version": "1.0.0"}


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    registry = default_registry(ROOT)
    for name in SUITE:
        (path,) = (ROOT / "scenarios" / "invoice" / "v1").rglob(f"{name}.yaml")
        store.put_scenario(compile_scenario(load_scenario_document(path.read_bytes()), registry))
    return World(clean_engine, store)


def client_for(world: World, **overrides: Any) -> TestClient:
    return TestClient(create_app(make_services(world.store, default_registry(ROOT), **overrides)))


@pytest.fixture
def client(world: World) -> TestClient:
    return client_for(world)


def body(scenarios: list[str] | None = None, seeds: list[str] | None = None, **kw: Any) -> Any:
    doc = {
        "schema_version": "1.0",
        "name": "demo",
        "scenario_refs": [{"id": s, "version": 1} for s in (scenarios or SUITE)],
        "baseline_agent": BASELINE,
        "candidate_agent": CANDIDATE,
        "seeds": seeds or ["42"],
        "trials_per_seed": 1,
        "mode": "mock",
        "gate_policy_id": "mock-release.v1",
    }
    doc.update(kw)
    return doc


def post(client: TestClient, doc: Any, key: str | None = "k1") -> Any:
    headers = {**AUTH, **({"Idempotency-Key": key} if key else {})}
    if isinstance(doc, bytes):
        return client.post("/api/v1/experiments", content=doc, headers=headers)
    return client.post("/api/v1/experiments", json=doc, headers=headers)


def counts(world: World) -> tuple[int, int, int, int]:
    return (
        world.one("SELECT count(*) FROM experiments"),
        world.one("SELECT count(*) FROM runs"),
        world.one("SELECT count(*) FROM experiment_slots"),
        world.one("SELECT count(*) FROM jobs WHERE kind = 'execute'"),
    )


def test_the_demo_matrix_is_created_atomically_with_twenty_runs(
    world: World, client: TestClient
) -> None:
    made = post(client, body())
    assert made.status_code == 202
    exp = made.json()
    assert made.headers["location"] == f"/api/v1/experiments/{exp['id']}"
    assert (exp["status"], exp["expected_runs"], exp["gate_policy_id"]) == (
        "running",
        20,
        "mock-release.v1",
    )
    assert exp["progress"]["baseline"] == {
        "total": 10,
        "queued": 10,
        "running": 0,
        "evaluating": 0,
        "completed": 0,
        "error": 0,
    }
    assert counts(world) == (1, 20, 20, 20)
    assert len({s["run_id"] for s in exp["slots"]}) == 20
    assert {s["seed"] for s in exp["slots"]} == {"42"}
    assert world.one("SELECT count(*) FROM runs WHERE experiment_id = :e", e=UUID(exp["id"])) == 20
    # One provenance snapshot for the whole matrix, and each cell's pair is comparable.
    assert world.one("SELECT count(DISTINCT manifest->'source') FROM runs") == 1
    assert world.one("SELECT count(DISTINCT manifest->>'grader_set_hash') FROM runs") <= 10
    roles = world.one(
        "SELECT count(*) FROM experiment_slots s JOIN runs r ON r.id = s.run_id "
        "WHERE (s.agent_role = 'baseline' AND r.agent_id = 'invoice-naive') "
        "OR (s.agent_role = 'candidate' AND r.agent_id = 'invoice-resilient')"
    )
    assert roles == 20


def test_creation_is_idempotent_and_conflicts_on_a_changed_matrix(
    world: World, client: TestClient
) -> None:
    first = post(client, body(), key="same")
    again = post(client, body(), key="same")
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert [s["run_id"] for s in again.json()["slots"]] == [
        s["run_id"] for s in first.json()["slots"]
    ]
    assert counts(world) == (1, 20, 20, 20)
    assert post(client, body(seeds=["43"]), key="same").status_code == 409
    assert post(client, body(), key=None).status_code == 400
    assert counts(world) == (1, 20, 20, 20)


def test_the_whole_batch_must_fit_under_the_queue_cap_or_nothing_is_written(
    world: World,
) -> None:
    tight = client_for(world, queue_cap=19)
    refused = post(tight, body(), key="k")
    assert refused.status_code == 429
    assert counts(world) == (0, 0, 0, 0)  # not even a partial matrix
    exact = client_for(world, queue_cap=20)
    assert post(exact, body(), key="k").status_code == 202  # same key: the refusal burned nothing
    assert post(exact, body(), key="other").status_code == 429  # the queue is now full
    assert counts(world) == (1, 20, 20, 20)


def test_queued_runs_already_waiting_count_against_the_batch(world: World) -> None:
    from .helpers import submit_run

    for _ in range(3):
        submit_run(world.store)
    assert post(client_for(world, queue_cap=22), body()).status_code == 429  # 3 + 20 > 22
    assert post(client_for(world, queue_cap=23), body()).status_code == 202


def test_two_matrices_racing_for_one_slot_in_the_queue_cannot_both_win(world: World) -> None:
    barrier = threading.Barrier(2)

    def attempt(key: str) -> int:
        client = client_for(world, queue_cap=25)
        barrier.wait()
        return int(post(client, body(), key=key).status_code)

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(attempt, ["a", "b"]))
    assert codes == [202, 429]
    assert counts(world) == (1, 20, 20, 20)


def test_validation_failures_create_nothing(world: World, client: TestClient) -> None:
    cases: list[tuple[Any, int, str | None]] = [
        (body(scenarios=["invoice-429", "no-such-scenario"]), 422, "/scenario_refs/1"),
        (
            body(baseline_agent={"id": "invoice-evil", "version": "1.0.0"}),
            422,
            "/baseline_agent/id",
        ),
        (body(candidate_agent={**CANDIDATE, "config": {"model": "x"}}), 422, "/candidate_agent"),
        (body(scenarios=["invoice-429", "invoice-429"]), 422, "/"),
        (body(seeds=["1", "2", "3", "4", "5", "6", "7"]), 422, "/"),
        (body(trials_per_seed=2), 422, "/trials_per_seed"),
        (body(mode="live"), 422, "/mode"),
        (body(extra=1), 422, "/extra"),
        (b"not json", 422, None),
    ]
    for doc, status, path in cases:
        response = post(client, doc, key=f"k-{uuid4()}")
        assert response.status_code == status, (doc, response.text)
        if path is not None:
            paths = {d["path"] for d in response.json()["error"]["details"]}
            assert any(p.startswith(path) for p in paths) or path == "/", (path, paths)
    assert counts(world) == (0, 0, 0, 0)


def test_an_incomparable_pair_is_refused_before_anything_is_written(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentcrash.api.app.incompatibilities", lambda a, b: ["GRADER_SET_MISMATCH"]
    )
    refused = post(client, body())
    assert refused.status_code == 422
    assert [d["code"] for d in refused.json()["error"]["details"]][0] == "GRADER_SET_MISMATCH"
    assert counts(world) == (0, 0, 0, 0)


def test_unknown_experiments_are_404_and_slots_are_immutable(
    world: World, client: TestClient
) -> None:
    assert client.get(f"/api/v1/experiments/{uuid4()}", headers=AUTH).status_code == 404
    assert client.get("/api/v1/experiments/nope", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/experiments/{uuid4()}").status_code == 401
    post(client, body(scenarios=["invoice-429"]))
    for statement in (
        "UPDATE experiment_slots SET trial_index = 2",
        "DELETE FROM experiment_slots",
    ):
        with pytest.raises(DBAPIError), world.engine.begin() as conn:
            conn.execute(text(statement))


def test_slots_follow_the_matrix_for_several_seeds(world: World, client: TestClient) -> None:
    exp = post(client, body(scenarios=["invoice-429", "invoice-503"], seeds=["1", "2"])).json()
    assert exp["expected_runs"] == 8
    assert sorted((s["scenario_id"], s["seed"], s["role"]) for s in exp["slots"]) == sorted(
        (sid, seed, role)
        for sid in ("invoice-429", "invoice-503")
        for seed in ("1", "2")
        for role in ("baseline", "candidate")
    )
    assert exp["progress"]["candidate"]["total"] == 4


def test_the_demo_runs_end_to_end_and_progress_follows_the_worker(
    world: World, client: TestClient
) -> None:
    exp = post(client, body()).json()
    world.drain(world.worker(), limit=60)
    done = client.get(f"/api/v1/experiments/{exp['id']}", headers=AUTH).json()
    assert done["status"] == "completed"
    assert done["progress"]["baseline"]["completed"] == 10
    assert done["progress"]["candidate"]["completed"] == 10
    verdicts = {(s["role"], s["scenario_id"]): s["verdict"] for s in done["slots"]}
    assert all(v == "pass" for (role, _), v in verdicts.items() if role == "candidate")
    baseline = [v for (role, _), v in verdicts.items() if role == "baseline"]
    assert "fail" in baseline and "pass" in baseline  # the weak agent fails some, not all


def test_experiment_status_is_derived_from_its_runs(world: World, client: TestClient) -> None:
    exp = post(client, body(scenarios=["invoice-429"])).json()
    runs = [s["run_id"] for s in exp["slots"]]

    def status_of(*, completed: list[str], errored: list[str] | None = None) -> str:
        with world.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET status = 'queued', error_code = NULL"))
            for run_id in completed:
                conn.execute(
                    text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": run_id}
                )
            for run_id in errored or []:
                conn.execute(
                    text("UPDATE runs SET status = 'error', error_code = 'X' WHERE id = :r"),
                    {"r": run_id},
                )
        got = client.get(f"/api/v1/experiments/{exp['id']}", headers=AUTH).json()
        return str(got["status"])

    assert status_of(completed=[]) == "running"
    assert status_of(completed=runs[:1]) == "running"  # some finished is still running
    assert status_of(completed=runs[:1], errored=runs[1:]) == "error"  # all terminal, one errored
    assert status_of(completed=runs) == "completed"
