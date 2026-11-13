"""Export and strict-replay jobs through the HTTP API, the queue and the worker, against real
PostgreSQL: idempotent creation, closed-attempt rule, content-addressed artifact with pin metadata,
replay results stored without touching the source evidence, redelivery and poison handling."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from agentcrash.agents.registry import ADAPTERS
from agentcrash.bootstrap import default_registry
from agentcrash.replay.bundle import read_bundle
from agentcrash.replay.service import check_replayable
from agentcrash.storage.exports import ExportRepo
from agentcrash.storage.store import PostgresStore
from agentcrash.workers.artifacts import ArtifactJobs
from agentcrash.workers.loop import Worker

from .helpers import ROOT, submit_run
from .test_api_flow import AUTH, make_services
from .test_execution import World

EVIDENCE_TABLES = [
    "runs",
    "run_attempts",
    "events",
    "tool_calls",
    "command_receipts",
    "ledger_entries",
    "evaluations",
]


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


@pytest.fixture
def client(world: World) -> TestClient:
    services = make_services(
        world.store,
        default_registry(ROOT),
        replay_check=lambda manifest: check_replayable(manifest, ADAPTERS),
    )
    from agentcrash.api.app import create_app

    return TestClient(create_app(services))


def artifact_worker(world: World, directory: Path) -> Worker:
    worker = world.worker()
    worker.artifacts = ArtifactJobs(world.repo, ExportRepo(world.engine), directory, ADAPTERS)
    return worker


def record(world: World, scenario: str = "invoice-429", agent: str = "invoice-resilient") -> Any:
    run_id = submit_run(world.store, scenario=scenario, agent=agent)
    world.drain(world.worker())  # executes and grades; no artifact worker yet
    (attempt,) = world.attempts(run_id)
    return run_id, attempt.id


def post(
    client: TestClient, route: str, body: dict[str, Any] | bytes, key: str | None = "k1"
) -> Any:
    headers = {**AUTH, **({"Idempotency-Key": key} if key else {})}
    if isinstance(body, bytes):
        return client.post(route, content=body, headers=headers)
    return client.post(route, json=body, headers=headers)


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def evidence_fingerprint(world: World) -> dict[str, Any]:
    with world.engine.connect() as conn:
        return {
            t: conn.execute(
                text(f"SELECT count(*), md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x")  # noqa: S608
            ).one()
            for t in EVIDENCE_TABLES
        }


# --- exports ----------------------------------------------------------------------------------


def test_an_export_is_created_idempotently_then_completed_by_the_worker(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id, attempt = record(world)
    route = f"/api/v1/runs/{run_id}/exports"
    first = post(client, route, {"attempt_id": str(attempt)})
    assert first.status_code == 202
    created = first.json()
    assert created["status"] == "queued" and created["pin"] is True
    assert created["artifact_hash"] is None
    assert first.headers["location"] == f"/api/v1/exports/{created['id']}"

    again = post(client, route, {"attempt_id": str(attempt)})
    assert again.status_code == 202 and again.json()["id"] == created["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert world.one("SELECT count(*) FROM exports") == 1
    changed = post(client, route, {"attempt_id": str(attempt), "pin": False})
    assert changed.status_code == 409  # same key, different body

    assert client.get(f"/api/v1/exports/{created['id']}", headers=AUTH).json()["status"] == "queued"
    world.drain(artifact_worker(world, tmp_path / "art"))
    done = client.get(f"/api/v1/exports/{created['id']}", headers=AUTH).json()
    assert done["status"] == "completed" and done["error_code"] is None
    digest = done["artifact_hash"]

    path = tmp_path / "art" / f"{digest}.acbundle"
    bundle = read_bundle(path)  # a valid, checksummed bundle named by its own hash
    assert bundle.bundle_sha256 == digest
    assert bundle.manifest.scenario.id == "invoice-429"
    assert [e.seq for e in bundle.events] == list(range(1, len(bundle.events) + 1))
    assert [e["verdict"] for e in bundle.evaluations] == ["pass"]
    assert (
        world.one("SELECT byte_size FROM artifacts WHERE hash = :h", h=digest)
        == path.stat().st_size
    )
    assert world.one("SELECT pin_until = 'infinity' FROM artifact_refs WHERE hash = :h", h=digest)
    assert world.one("SELECT status FROM jobs WHERE kind = 'export'") == "done"


def test_an_unpinned_export_records_no_pin_and_the_same_evidence_shares_one_artifact(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id, attempt = record(world)
    route = f"/api/v1/runs/{run_id}/exports"
    a = post(client, route, {"attempt_id": str(attempt), "pin": False}, key="a").json()
    b = post(client, route, {"attempt_id": str(attempt)}, key="b").json()
    world.drain(artifact_worker(world, tmp_path / "art"))
    hashes = {
        client.get(f"/api/v1/exports/{x['id']}", headers=AUTH).json()["artifact_hash"]
        for x in (a, b)
    }
    assert len(hashes) == 1  # identical evidence: one content-addressed file
    assert world.one("SELECT count(*) FROM artifacts") == 1
    assert world.one("SELECT count(*) FROM artifact_refs") == 2
    assert world.one(
        "SELECT pin_until IS NULL FROM artifact_refs WHERE owner_id = :i", i=UUID(a["id"])
    )
    assert world.one(
        "SELECT pin_until = 'infinity' FROM artifact_refs WHERE owner_id = :i", i=UUID(b["id"])
    )
    assert len(list((tmp_path / "art").iterdir())) == 1


def test_an_active_attempt_cannot_be_exported_and_the_refusal_does_not_burn_the_key(
    world: World, client: TestClient
) -> None:
    run_id = submit_run(world.store)
    claim = world.queue.claim_execute("w1")  # the attempt is now open
    assert claim is not None
    route = f"/api/v1/runs/{run_id}/exports"
    refused = post(client, route, {"attempt_id": str(claim.attempt_id)}, key="same")
    assert refused.status_code == 409 and error_code(refused) == "ATTEMPT_NOT_CLOSED"
    assert world.one("SELECT count(*) FROM exports") == 0
    world.queue.close_attempt(claim.attempt_id, claim.fencing_token, "agent_error")
    ok = post(client, route, {"attempt_id": str(claim.attempt_id)}, key="same")
    assert ok.status_code == 202  # same key, now that the attempt is closed


@pytest.mark.parametrize("kind", ["exports", "replays"])
def test_creation_requests_are_validated(world: World, client: TestClient, kind: str) -> None:
    run_id, attempt = record(world)
    other_run, other_attempt = record(world, "invoice-503")
    route = f"/api/v1/runs/{run_id}/{kind}"
    ok: dict[str, Any] = {"attempt_id": str(attempt)} | (
        {"mode": "strict"} if kind == "replays" else {}
    )

    assert post(client, route, ok, key=None).status_code == 400  # key required
    assert error_code(post(client, route, ok, key=None)) == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(client, route, ok, key="bad\tkey").status_code == 400
    assert post(client, f"/api/v1/runs/{uuid4()}/{kind}", ok).status_code == 404
    assert post(client, f"/api/v1/runs/not-a-uuid/{kind}", ok).status_code == 404
    wrong = post(client, route, {**ok, "attempt_id": str(other_attempt)})
    assert wrong.status_code == 422  # the attempt belongs to another run
    for body in (
        {},
        {**ok, "extra": 1},
        {**ok, "attempt_id": "nope"},
        {**ok, "attempt_id": 5},
        {**ok, "pin": "yes"} if kind == "exports" else {**ok, "mode": "loose"},
        b"not json",
    ):
        assert post(client, route, body, key=f"k-{uuid4()}").status_code == 422, body
    assert world.one(f"SELECT count(*) FROM {kind}") == 0  # noqa: S608 - fixed names
    assert other_run != run_id


# --- strict replays ---------------------------------------------------------------------------


def test_a_replay_runs_in_the_worker_and_changes_no_source_evidence(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id, attempt = record(world)
    before = evidence_fingerprint(world)
    created = post(
        client, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt)}
    )
    assert created.status_code == 202 and created.json()["status"] == "queued"
    replay_id = created.json()["id"]
    again = post(
        client, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt)}
    )
    assert again.json()["id"] == replay_id and again.headers["idempotent-replayed"] == "true"

    world.drain(artifact_worker(world, tmp_path))
    done = client.get(f"/api/v1/replays/{replay_id}", headers=AUTH).json()
    assert (
        done["status"] == "completed" and done["divergence"] is None and done["error_code"] is None
    )
    assert done["consumed_events"] == done["total_events"] > 1
    assert len(done["adapter_hash"]) == 64 and done["mode"] == "strict"
    assert evidence_fingerprint(world) == before  # replay wrote nothing to the source evidence


def test_a_divergence_is_stored_and_served(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id, attempt = record(world)
    done: dict[str, Any] = {}
    # A different seed changes the seeded retry jitter, so the recorded wait no longer matches.
    for seed in ("9999", "1", "2", "3"):
        with world.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE runs SET manifest = jsonb_set("
                    "manifest, '{seed}', to_jsonb(CAST(:s AS text)))"
                ),
                {"s": seed},
            )
        created = post(
            client,
            f"/api/v1/runs/{run_id}/replays",
            {"mode": "strict", "attempt_id": str(attempt)},
            key=f"seed-{seed}",
        )
        world.drain(artifact_worker(world, tmp_path))
        done = client.get(f"/api/v1/replays/{created.json()['id']}", headers=AUTH).json()
        if done["divergence"] is not None:
            break
    assert done["status"] == "completed"
    assert done["divergence"]["code"] == "REPLAY_DIVERGENCE"
    assert done["divergence"]["kind"] == "sleep_changed"
    assert done["consumed_events"] < done["total_events"]


def test_an_incompatible_run_is_refused_at_creation_with_reasons(
    world: World, client: TestClient
) -> None:
    run_id, attempt = record(world)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET manifest = "
                "jsonb_set(manifest, '{replay_capability}', '\"partial\"')"
            )
        )
    refused = post(
        client, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt)}
    )
    assert refused.status_code == 409 and error_code(refused) == "REPLAY_INCOMPATIBLE"
    assert [d["code"] for d in refused.json()["error"]["details"]] == ["REPLAY_CAPABILITY_PARTIAL"]
    assert (
        world.one("SELECT count(*) FROM replays") == 0
        and world.one("SELECT count(*) FROM jobs WHERE kind = 'replay'") == 0
    )


def test_services_built_without_a_replay_check_refuse_every_replay(world: World) -> None:
    from agentcrash.api.app import create_app

    bare = TestClient(create_app(make_services(world.store, default_registry(ROOT))))
    run_id, attempt = record(world)
    refused = post(
        bare, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt)}
    )
    assert refused.status_code == 409 and error_code(refused) == "REPLAY_INCOMPATIBLE"


def test_an_attempt_without_a_terminal_result_ends_the_replay_with_a_stable_code(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id = submit_run(
        world.store,
        scenario="invoice-healthy",
        agent="invoice-resilient",
        budgets={"tool_calls": 2},
    )
    world.drain(world.worker())
    (attempt,) = world.attempts(run_id)
    created = post(
        client, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt.id)}
    )
    world.drain(artifact_worker(world, tmp_path))
    done = client.get(f"/api/v1/replays/{created.json()['id']}", headers=AUTH).json()
    assert (done["status"], done["error_code"]) == ("error", "NOT_REPLAYABLE")


def test_unknown_export_and_replay_ids_are_404(client: TestClient) -> None:
    for route in ("exports", "replays"):
        assert client.get(f"/api/v1/{route}/{uuid4()}", headers=AUTH).status_code == 404
        assert client.get(f"/api/v1/{route}/nope", headers=AUTH).status_code == 404
        assert client.get(f"/api/v1/{route}/{uuid4()}").status_code == 401


# --- queue behavior ---------------------------------------------------------------------------


def test_without_an_artifact_worker_export_jobs_stay_queued(
    world: World, client: TestClient
) -> None:
    run_id, attempt = record(world)
    post(client, f"/api/v1/runs/{run_id}/exports", {"attempt_id": str(attempt)})
    world.drain(world.worker())
    assert world.one("SELECT status FROM jobs WHERE kind = 'export'") == "queued"
    assert world.one("SELECT status FROM exports") == "queued"


def test_an_expired_export_lease_is_requeued_and_completes_once(
    world: World, client: TestClient, tmp_path: Path
) -> None:
    run_id, attempt = record(world)
    post(client, f"/api/v1/runs/{run_id}/exports", {"attempt_id": str(attempt)})
    claim = world.queue.claim_subject("export", "dead-worker")
    assert claim is not None
    assert world.one("SELECT status FROM exports") == "running"
    with world.engine.begin() as conn:  # the worker died holding the lease
        conn.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 minute' WHERE kind = 'export'")
        )
    world.queue.reap_expired()
    assert world.one("SELECT status FROM jobs WHERE kind = 'export'") == "queued"
    world.drain(artifact_worker(world, tmp_path))
    assert world.one("SELECT status FROM exports") == "completed"
    assert world.one("SELECT count(*) FROM artifacts") == 1
    # the dead worker waking up later cannot publish over the finished job
    assert not ExportRepo(world.engine).complete_export(
        claim.job_id, claim.subject_id, "0" * 64, 1, "x"
    )
    assert world.one("SELECT count(*) FROM artifacts") == 1


def test_a_job_delivered_too_often_fails_visibly(world: World, client: TestClient) -> None:
    run_id, attempt = record(world)
    post(client, f"/api/v1/runs/{run_id}/replays", {"mode": "strict", "attempt_id": str(attempt)})
    with world.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET delivery_count = 3 WHERE kind = 'replay'"))
    assert world.queue.claim_subject("replay", "w") is None
    assert world.one("SELECT status FROM jobs WHERE kind = 'replay'") == "error"
    replay_id = world.one("SELECT id FROM replays")
    replay = client.get(f"/api/v1/replays/{replay_id}", headers=AUTH).json()
    assert (replay["status"], replay["error_code"]) == ("error", "JOB_FAILED")


def test_the_one_active_execute_job_rule_ignores_export_and_replay_jobs(
    world: World, client: TestClient
) -> None:
    run_id, attempt = record(world)
    for i in range(3):
        post(
            client,
            f"/api/v1/runs/{run_id}/exports",
            {"attempt_id": str(attempt), "pin": bool(i % 2)},
            key=f"e{i}",
        )
    assert world.one("SELECT count(*) FROM jobs WHERE kind = 'export'") == 3
