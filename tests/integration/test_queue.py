"""Job claim, lease, heartbeat, reaper and fencing against real PostgreSQL.

Races use a barrier, and lease expiry is forced by rewriting ``lease_until`` in the database, so
nothing here sleeps or depends on wall-clock timing.
"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from agentcrash.domain.ports import Claim, StaleFence
from agentcrash.storage.queue import JobQueue, lock_fenced_attempt

from .helpers import run_concurrently


@pytest.fixture
def queue(clean_engine: Engine) -> JobQueue:
    return JobQueue(clean_engine)


def seed_runs(engine: Engine, n: int = 1) -> list[UUID]:
    """``n`` queued runs, each with its execute job, inserted directly (the API path is
    covered elsewhere; here only the queue tables matter)."""
    ids = [uuid4() for _ in range(n)]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scenario_versions (id, version, content_hash, schema_version,"
                " normalized_json) VALUES ('s', 1, 'h', '1.0', '{}') ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_versions (id, version, adapter_registry_key,"
                " config_schema_hash) VALUES ('a', '1', 'k', 'h') ON CONFLICT DO NOTHING"
            )
        )
        for run_id in ids:
            conn.execute(
                text(
                    "INSERT INTO runs (id, scenario_id, scenario_version, agent_id, agent_version,"
                    " seed, manifest, manifest_hash) VALUES (:r, 's', 1, 'a', '1', 1, '{}', 'm')"
                ),
                {"r": run_id},
            )
            conn.execute(
                text(
                    "INSERT INTO jobs (id, kind, run_id, dedupe_key) VALUES (:j, 'execute', :r, :d)"
                ),
                {"j": uuid4(), "r": run_id, "d": f"execute:{run_id}"},
            )
    return ids


def scalar(engine: Engine, sql: str, **params: object) -> object:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def expire(engine: Engine, job_id: UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = :j"),
            {"j": job_id},
        )


def claim_one(queue: JobQueue, owner: str = "w1") -> Claim:
    claim = queue.claim_execute(owner)
    assert claim is not None
    return claim


def test_claim_creates_a_fresh_attempt_and_leases_the_job(
    queue: JobQueue, clean_engine: Engine
) -> None:
    (run_id,) = seed_runs(clean_engine)
    claim = claim_one(queue)
    assert (claim.run_id, claim.ordinal, claim.fencing_token) == (run_id, 1, 1)
    assert scalar(clean_engine, "SELECT status FROM runs WHERE id = :r", r=run_id) == "running"
    assert scalar(clean_engine, "SELECT fencing_token FROM runs WHERE id = :r", r=run_id) == 1
    with clean_engine.connect() as conn:  # closed explicitly: an open tx would block TRUNCATE
        row = conn.execute(
            text("SELECT status, owner, fencing_token, delivery_count, attempt_id FROM jobs")
        ).one()
    assert tuple(row) == ("leased", "w1", 1, 1, claim.attempt_id)
    assert scalar(clean_engine, "SELECT count(*) FROM run_attempts") == 1
    assert queue.claim_execute("w2") is None  # leased jobs are not claimable


def test_empty_queue_claims_nothing(queue: JobQueue) -> None:
    assert queue.claim_execute("w1") is None


def test_two_workers_race_for_one_job_and_exactly_one_gets_an_attempt(
    queue: JobQueue, clean_engine: Engine
) -> None:
    seed_runs(clean_engine)
    results = run_concurrently(8, lambda i: queue.claim_execute(f"w{i}"))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert scalar(clean_engine, "SELECT count(*) FROM run_attempts") == 1
    assert scalar(clean_engine, "SELECT delivery_count FROM jobs") == 1


def test_concurrent_workers_get_distinct_jobs(queue: JobQueue, clean_engine: Engine) -> None:
    seed_runs(clean_engine, 6)
    results = run_concurrently(6, lambda i: queue.claim_execute(f"w{i}"))
    claims = [r for r in results if r is not None]
    assert len(claims) == 6
    assert len({c.run_id for c in claims}) == len({c.job_id for c in claims}) == 6
    assert scalar(clean_engine, "SELECT count(*) FROM run_attempts") == 6


def test_a_job_for_a_finished_run_is_never_claimed(queue: JobQueue, clean_engine: Engine) -> None:
    """Duplicate delivery after the run finished must be a no-op."""
    seed_runs(clean_engine)
    with clean_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed'"))
    assert queue.claim_execute("w1") is None
    assert scalar(clean_engine, "SELECT count(*) FROM run_attempts") == 0


def test_heartbeat_extends_only_for_the_matching_owner_and_token(
    queue: JobQueue, clean_engine: Engine
) -> None:
    seed_runs(clean_engine)
    claim = claim_one(queue)
    with clean_engine.begin() as conn:  # shorten the lease so an extension is visible
        conn.execute(text("UPDATE jobs SET lease_until = now() + interval '2 seconds'"))
    before = scalar(clean_engine, "SELECT lease_until FROM jobs")
    assert queue.heartbeat(claim.job_id, "intruder", claim.fencing_token) is False
    assert queue.heartbeat(claim.job_id, claim.owner, claim.fencing_token + 1) is False
    assert scalar(clean_engine, "SELECT lease_until FROM jobs") == before
    assert queue.heartbeat(claim.job_id, claim.owner, claim.fencing_token) is True
    assert scalar(clean_engine, "SELECT lease_until FROM jobs") > before  # type: ignore[operator]


def test_heartbeat_cannot_revive_an_expired_lease(queue: JobQueue, clean_engine: Engine) -> None:
    seed_runs(clean_engine)
    claim = claim_one(queue)
    expire(clean_engine, claim.job_id)
    assert queue.heartbeat(claim.job_id, claim.owner, claim.fencing_token) is False


def test_reaper_ignores_live_leases(queue: JobQueue, clean_engine: Engine) -> None:
    seed_runs(clean_engine)
    claim_one(queue)
    assert queue.reap_expired() == []
    assert scalar(clean_engine, "SELECT count(*) FROM run_attempts WHERE closed_at IS NULL") == 1


def test_reaper_closes_the_lost_attempt_and_queues_a_fresh_one(
    queue: JobQueue, clean_engine: Engine
) -> None:
    (run_id,) = seed_runs(clean_engine)
    first = claim_one(queue)
    expire(clean_engine, first.job_id)

    (reaped,) = queue.reap_expired()
    assert (reaped.run_id, reaped.attempt_id, reaped.ordinal) == (run_id, first.attempt_id, 1)
    assert reaped.requeued is True
    assert (
        scalar(
            clean_engine,
            "SELECT termination FROM run_attempts WHERE id = :a",
            a=first.attempt_id,
        )
        == "worker_lost"
    )
    assert scalar(clean_engine, "SELECT status FROM jobs WHERE id = :j", j=first.job_id) == "error"
    assert scalar(clean_engine, "SELECT status FROM runs WHERE id = :r", r=run_id) == "running"
    assert queue.reap_expired() == []  # idempotent

    second = claim_one(queue, "w2")
    assert second.run_id == run_id and second.ordinal == 2
    assert second.attempt_id != first.attempt_id
    assert second.fencing_token == 3  # claim=1, reaper invalidation=2, reclaim=3


def test_stale_worker_is_fenced_after_reassignment(queue: JobQueue, clean_engine: Engine) -> None:
    seed_runs(clean_engine)
    old = claim_one(queue)
    expire(clean_engine, old.job_id)
    queue.reap_expired()

    # Fenced as soon as the reaper acts, even before anyone reclaims the run.
    with pytest.raises(StaleFence):
        queue.close_attempt(old.attempt_id, old.fencing_token, "agent_completed")

    new = claim_one(queue, "w2")
    with pytest.raises(StaleFence):
        queue.close_attempt(old.attempt_id, old.fencing_token, "agent_completed")
    with pytest.raises(StaleFence):  # nor can it borrow the new holder's token
        queue.close_attempt(old.attempt_id, new.fencing_token, "agent_completed")
    with pytest.raises(StaleFence):  # nor close the new attempt with the old token
        queue.close_attempt(new.attempt_id, old.fencing_token, "agent_completed")
    assert queue.heartbeat(old.job_id, old.owner, old.fencing_token) is False
    assert (
        scalar(clean_engine, "SELECT termination FROM run_attempts WHERE id = :a", a=old.attempt_id)
        == "worker_lost"
    )

    queue.close_attempt(new.attempt_id, new.fencing_token, "agent_completed")
    with pytest.raises(StaleFence):  # an attempt closes once
        queue.close_attempt(new.attempt_id, new.fencing_token, "agent_error")


def test_fence_check_rejects_unknown_attempts(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn, pytest.raises(StaleFence):
        lock_fenced_attempt(conn, uuid4(), 1)


def test_workers_cannot_report_worker_lost_or_invented_terminations(
    queue: JobQueue, clean_engine: Engine
) -> None:
    seed_runs(clean_engine)
    claim = claim_one(queue)
    for bad in ("worker_lost", "passed", ""):
        with pytest.raises(ValueError, match="cannot be reported"):
            queue.close_attempt(claim.attempt_id, claim.fencing_token, bad)
    assert scalar(clean_engine, "SELECT termination FROM run_attempts") is None


def test_attempt_cap_turns_the_run_into_an_error(queue: JobQueue, clean_engine: Engine) -> None:
    (run_id,) = seed_runs(clean_engine)
    first = claim_one(queue)
    expire(clean_engine, first.job_id)
    queue.reap_expired()
    second = claim_one(queue, "w2")
    expire(clean_engine, second.job_id)

    (reaped,) = queue.reap_expired()
    assert (reaped.ordinal, reaped.requeued) == (2, False)
    assert scalar(clean_engine, "SELECT status FROM runs WHERE id = :r", r=run_id) == "error"
    assert scalar(clean_engine, "SELECT closed_at IS NOT NULL FROM runs") is True
    assert (
        scalar(clean_engine, "SELECT count(*) FROM jobs WHERE status IN ('queued','leased')") == 0
    )
    assert (
        scalar(clean_engine, "SELECT count(*) FROM run_attempts WHERE termination = 'worker_lost'")
        == 2
    )
    assert queue.claim_execute("w3") is None  # no third attempt, no silent continuation


def test_concurrent_reapers_requeue_exactly_once(queue: JobQueue, clean_engine: Engine) -> None:
    seed_runs(clean_engine)
    expire(clean_engine, claim_one(queue).job_id)
    results = run_concurrently(6, lambda _: queue.reap_expired())
    assert sum(len(r) for r in results) == 1
    assert scalar(clean_engine, "SELECT count(*) FROM jobs WHERE status = 'queued'") == 1
    assert scalar(clean_engine, "SELECT fencing_token FROM runs") == 2


def test_reaper_racing_a_fenced_close_never_lets_both_win(
    queue: JobQueue, clean_engine: Engine
) -> None:
    """Worker closes its attempt at the instant the reaper expires it: exactly one outcome."""
    seed_runs(clean_engine)
    claim = claim_one(queue)
    expire(clean_engine, claim.job_id)

    def act(i: int) -> str:
        if i == 0:
            try:
                queue.close_attempt(claim.attempt_id, claim.fencing_token, "agent_completed")
            except StaleFence:
                return "fenced"
            return "closed"
        return f"reaped:{len(queue.reap_expired())}"

    outcomes = run_concurrently(2, act)
    termination = scalar(clean_engine, "SELECT termination FROM run_attempts")
    if outcomes[0] == "closed":  # worker won the run lock; reaper then sees a closed attempt
        assert termination == "agent_completed" and outcomes[1] == "reaped:0"
    else:
        assert termination == "worker_lost" and outcomes[1] == "reaped:1"


def test_a_locked_job_does_not_block_other_workers(queue: JobQueue, clean_engine: Engine) -> None:
    """SKIP LOCKED: a claim in flight on the oldest job must not stall the next worker."""
    seed_runs(clean_engine, 2)
    # The pool is the outer context so a failure releases the row lock before joining the thread.
    with ThreadPoolExecutor(max_workers=1) as pool, clean_engine.begin() as conn:
        locked = conn.execute(
            text("SELECT run_id FROM jobs ORDER BY available_at, created_at, id LIMIT 1 FOR UPDATE")
        ).scalar_one()
        claim = pool.submit(queue.claim_execute, "w2").result(
            timeout=10
        )  # hangs without SKIP LOCKED
    assert claim is not None and claim.run_id != locked
