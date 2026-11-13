"""Reruns: a new, independent execution linked to its parent (never a replay), against real
PostgreSQL through the HTTP API and the worker."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from agentcrash.api.app import create_app
from agentcrash.bootstrap import default_registry
from agentcrash.storage.store import PostgresStore

from .helpers import ROOT, submit_run
from .test_api_flow import AUTH, make_services
from .test_execution import World


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(create_app(make_services(world.store, default_registry(ROOT))))


def rerun(client: TestClient, parent: Any, body: Any = None, key: str | None = "k1") -> Any:
    headers = {**AUTH, **({"Idempotency-Key": key} if key else {})}
    route = f"/api/v1/runs/{parent}/reruns"
    if body is None:
        return client.post(route, headers=headers)
    if isinstance(body, bytes):
        return client.post(route, content=body, headers=headers)
    return client.post(route, json=body, headers=headers)


def evidence_digest(world: World, run_id: UUID) -> Any:
    with world.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*), md5(string_agg(e::text, '|' ORDER BY e.attempt_id, e.seq)) "
                "FROM events e JOIN run_attempts a ON a.id = e.attempt_id WHERE a.run_id = :r"
            ),
            {"r": run_id},
        ).one()


def test_a_rerun_is_a_new_linked_run_with_the_parents_inputs(
    world: World, client: TestClient
) -> None:
    parent = submit_run(world.store, "invoice-429", "invoice-resilient", seed="42")
    with world.engine.begin() as conn:
        conn.execute(text("""UPDATE runs SET labels = '{"purpose": "demo"}'"""))
    made = rerun(client, parent)
    assert made.status_code == 202
    run = made.json()
    assert run["id"] != str(parent) and run["parent_run_id"] == str(parent)
    assert run["status"] == "queued" and made.headers["location"] == f"/api/v1/runs/{run['id']}"
    assert (run["scenario"], run["agent"], run["seed"]) == (
        {"id": "invoice-429", "version": 1},
        {"id": "invoice-resilient", "version": "1.0.0"},
        "42",
    )
    assert run["labels"] == {"purpose": "demo"}
    assert world.one("SELECT count(*) FROM replays") == 0  # a rerun is not a replay
    assert world.one("SELECT count(*) FROM jobs WHERE kind = 'execute'") == 2


def test_a_seed_override_changes_the_seed_and_the_manifest(
    world: World, client: TestClient
) -> None:
    parent = submit_run(world.store, "invoice-429", "invoice-resilient", seed="42")
    same = rerun(client, parent, key="same").json()
    other = rerun(client, parent, {"seed": "7"}, key="other").json()
    assert other["seed"] == "7" and same["seed"] == "42"
    assert other["manifest_hash"] != same["manifest_hash"]
    assert rerun(client, parent, {}, key="empty-object").json()["seed"] == "42"


def test_budgets_are_carried_from_the_parent(world: World, client: TestClient) -> None:
    parent = submit_run(world.store, "invoice-healthy", "invoice-naive", budgets={"tool_calls": 7})
    child = UUID(rerun(client, parent).json()["id"])
    budgets = world.one("SELECT manifest->'budgets' FROM runs WHERE id = :r", r=child)
    assert budgets["tool_calls"] == 7


def test_rerun_creation_is_idempotent_and_conflicts_on_a_changed_body(
    world: World, client: TestClient
) -> None:
    parent = submit_run(world.store, "invoice-429", "invoice-resilient")
    first = rerun(client, parent, {"seed": "9"}, key="k")
    again = rerun(client, parent, {"seed": "9"}, key="k")
    assert again.json()["id"] == first.json()["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert world.one("SELECT count(*) FROM runs") == 2
    assert rerun(client, parent, {"seed": "10"}, key="k").status_code == 409
    assert rerun(client, parent, None, key="k").status_code == 409  # no override is a body too
    assert rerun(client, parent, key=None).status_code == 400
    other_parent = submit_run(world.store, "invoice-503", "invoice-resilient")
    assert rerun(client, other_parent, {"seed": "9"}, key="k").status_code == 409  # key is per run


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"seed": "-1"}, "INVALID_SEED"),
        ({"seed": "007"}, "INVALID_SEED"),
        ({"seed": 42}, "INVALID_VALUE"),
        ({"agent": {"id": "invoice-evil"}}, "UNKNOWN_FIELD"),
        ({"budgets": {}}, "UNKNOWN_FIELD"),
        (b"not json", None),
    ],
)
def test_invalid_overrides_are_rejected_without_creating_anything(
    world: World, client: TestClient, body: Any, code: str | None
) -> None:
    parent = submit_run(world.store, "invoice-429", "invoice-resilient")
    refused = rerun(client, parent, body)
    assert refused.status_code == 422
    if code is not None:
        assert code in {d["code"] for d in refused.json()["error"]["details"]}
    assert world.one("SELECT count(*) FROM runs") == 1


def test_unknown_parents_are_404(client: TestClient) -> None:
    assert rerun(client, uuid4()).status_code == 404
    assert rerun(client, "not-a-uuid").status_code == 404


def test_a_rerun_executes_independently_and_leaves_the_parent_evidence_untouched(
    world: World, client: TestClient
) -> None:
    parent = submit_run(world.store, "invoice-429", "invoice-resilient")
    world.drain(world.worker())
    before = evidence_digest(world, parent)
    child = UUID(rerun(client, parent).json()["id"])
    chain = UUID(rerun(client, child, {"seed": "5"}, key="chain").json()["id"])
    world.drain(world.worker())
    assert evidence_digest(world, parent) == before  # the parent's events never change
    for run_id in (child, chain):
        assert world.run(run_id).status == "completed" and world.run(run_id).verdict == "pass"
        assert len(world.attempts(run_id)) == 1
    assert world.run(chain).parent_run_id == child  # linked to the immediate parent
    assert world.one("SELECT count(*) FROM run_attempts") == 3  # each run has its own attempt
