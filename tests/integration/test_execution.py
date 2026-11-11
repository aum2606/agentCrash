"""M2 acceptance against real PostgreSQL and real child processes.

Workers run in-process here (so tests can place hooks at exact points such as "after the write
commits"), but every agent runs in a real child process talking to the loopback gateway.
Nothing sleeps for synchronization: events, barriers and database state coordinate the tests.
"""

import threading
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from agentcrash.agents.registry import ADAPTERS
from agentcrash.api.app import create_app
from agentcrash.bootstrap import default_registry
from agentcrash.domain.events import chain_problem, observable_digest
from agentcrash.domain.ports import StaleFence
from agentcrash.environments.invoice import catalog
from agentcrash.execution.gateway import Hooks
from agentcrash.storage.attempts import AttemptRepo
from agentcrash.storage.queue import JobQueue
from agentcrash.storage.store import PostgresStore
from agentcrash.workers.loop import Worker
from agentcrash.workers.supervisor import AttemptSupervisor, SupervisorConfig

from .helpers import ROOT, submit_run
from .test_api_flow import AUTH, make_services

FAST = SupervisorConfig(heartbeat_s=3600, cancel_poll_s=0.05, tick_s=0.02, kill_grace_s=1.0)


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> "World":
    return World(clean_engine, store)


class World:
    def __init__(self, engine: Engine, store: PostgresStore) -> None:
        self.engine = engine
        self.store = store
        self.queue = JobQueue(engine)
        self.repo = AttemptRepo(engine)

    def worker(
        self, owner: str = "w1", hooks: Hooks | None = None, config: SupervisorConfig = FAST
    ) -> Worker:
        sup = AttemptSupervisor(
            self.queue, self.repo, ROOT, owner, dict(catalog.TOOLS), config, hooks or Hooks()
        )
        return Worker(self.queue, self.repo, sup, owner)

    def drain(self, worker: Worker, limit: int = 10) -> None:
        for _ in range(limit):
            if not worker.run_once():
                return
        raise AssertionError("worker never went idle")

    def one(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def run(self, run_id: UUID) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT * FROM runs WHERE id = :r"), {"r": run_id}).one()

    def attempts(self, run_id: UUID) -> list[Any]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT * FROM run_attempts WHERE run_id = :r ORDER BY ordinal"),
                    {"r": run_id},
                )
            )

    def events(self, attempt_id: UUID) -> list[Any]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT * FROM events WHERE attempt_id = :a ORDER BY seq"),
                    {"a": attempt_id},
                )
            )

    def api(self) -> TestClient:
        services = make_services(self.store, default_registry(ROOT))
        return TestClient(create_app(services))


def event_types(world: World, attempt_id: UUID) -> list[str]:
    return [e.type for e in world.events(attempt_id)]


def stored(world: World, attempt_id: UUID) -> Any:
    manifest_budgets = world.repo.manifest(
        world.one("SELECT run_id FROM run_attempts WHERE id = :a", a=attempt_id)
    ).budgets
    return world.repo.evidence(attempt_id, manifest_budgets, {})


# --- the healthy path ---------------------------------------------------------------------


def test_healthy_run_completes_with_a_pass_and_complete_evidence(world: World) -> None:
    run_id = submit_run(world.store)
    assert world.run(run_id).status == "queued"  # accepting a run executes nothing

    world.drain(world.worker())

    run = world.run(run_id)
    assert (run.status, run.verdict, run.error_code) == ("completed", "pass", None)
    (attempt,) = world.attempts(run_id)
    assert attempt.termination == "agent_completed"
    assert run.selected_attempt_id == attempt.id

    events = world.events(attempt.id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # contiguous
    assert {e.run_id for e in events} == {run_id}  # attributable
    assert events[0].type == "attempt.started" and events[-1].type == "attempt.closed"
    types = [e.type for e in events]
    assert types.count("tool.requested") == types.count("policy.decided") == 4
    assert types.count("tool.completed") == 4 and types.count("environment.mutated") == 1
    ev = stored(world, attempt.id)
    assert chain_problem(ev.events) is None
    assert ev.chain_head == events[-1].event_hash

    assert world.one("SELECT count(*) FROM ledger_entries WHERE attempt_id = :a", a=attempt.id) == 1
    assert (
        world.one("SELECT count(*) FROM command_receipts WHERE attempt_id = :a", a=attempt.id) == 1
    )
    grades = world.one("SELECT grades FROM evaluations WHERE attempt_id = :a", a=attempt.id)
    assert {g["grader_id"]: g["result"] for g in grades} == {
        "trace.complete.v1": "pass",
        "invoice.correct_post.v1": "pass",
        "invoice.no_duplicate.v1": "pass",
        "invoice.no_unauthorized.v1": "pass",
        "invoice.fresh_source.v1": "pass",
        "budget.within_limits.v1": "pass",
    }
    assert (
        world.one(
            "SELECT job_status FROM (SELECT string_agg(status, ',' ORDER BY kind) "
            "AS job_status FROM jobs) j"
        )
        == "done,done"
    )


def test_run_survives_an_api_restart_and_exposes_its_evidence(world: World) -> None:
    first = world.api()
    scenario = (ROOT / "scenarios/invoice/v1/invoice-healthy.yaml").read_bytes()
    assert (
        first.post(
            "/api/v1/scenarios",
            content=scenario,
            headers={**AUTH, "Content-Type": "application/yaml"},
        ).status_code
        == 201
    )
    world.store.ensure_agent_version("invoice-naive", "1.0.0", "invoice-naive", "h", None)
    created = first.post(
        "/api/v1/runs",
        json={
            "schema_version": "1.0",
            "scenario": {"id": "invoice-healthy", "version": 1},
            "agent": {"id": "invoice-naive", "version": "1.0.0", "config": {}},
            "seed": "7",
            "mode": "mock",
        },
        headers={**AUTH, "Idempotency-Key": "k-1"},
    )
    assert created.status_code == 202 and created.json()["status"] == "queued"
    run_id = UUID(created.json()["id"])
    del first  # the "restart": a brand-new app over the same database
    world.drain(world.worker())
    second = world.api()
    run = second.get(f"/api/v1/runs/{run_id}", headers=AUTH).json()
    assert (run["status"], run["verdict"]) == ("completed", "pass")
    assert run["attempts"][0]["termination"] == "agent_completed"

    page = second.get(f"/api/v1/runs/{run_id}/events?limit=3", headers=AUTH).json()
    assert page["has_more"] and [e["seq"] for e in page["items"]] == [1, 2, 3]
    rest = second.get(
        f"/api/v1/runs/{run_id}/events?after_seq={page['next_after_seq']}&limit=200", headers=AUTH
    ).json()
    assert rest["attempt_closed"] and not rest["has_more"]
    assert rest["items"][-1]["type"] == "attempt.closed"

    evaluations = second.get(f"/api/v1/runs/{run_id}/evaluations", headers=AUTH).json()["items"]
    assert [e["verdict"] for e in evaluations] == ["pass"]
    assert second.get(f"/api/v1/runs/{uuid4()}/events", headers=AUTH).status_code == 404
    bad = second.get(f"/api/v1/runs/{run_id}/events?attempt_id={uuid4()}", headers=AUTH)
    assert (
        bad.status_code == 422 and bad.json()["error"]["details"][0]["code"] == "ATTEMPT_NOT_IN_RUN"
    )


def test_same_manifest_gives_the_same_observable_digest_and_state(world: World) -> None:
    ids = [submit_run(world.store, seed="99") for _ in range(2)]
    world.drain(world.worker(), limit=20)
    digests, states = [], []
    for run_id in ids:
        (attempt,) = world.attempts(run_id)
        digests.append(observable_digest(stored(world, attempt.id).events))
        states.append(attempt.final_state_hash)
    assert digests[0] == digests[1] and states[0] == states[1]
    assert len(set(digests)) == 1


# --- unsupported work fails loudly, never as a fake pass ------------------------------------


def test_an_agent_without_an_adapter_ends_in_error_not_a_pass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both reference agents run now; withdraw one to prove the guard still fails closed.
    monkeypatch.delitem(ADAPTERS, ("invoice-resilient", "1.0.0"))
    run_id = submit_run(world.store, scenario="invoice-healthy", agent="invoice-resilient")
    world.drain(world.worker())
    run = world.run(run_id)
    assert (run.status, run.verdict, run.error_code) == ("error", None, "ADAPTER_NOT_IMPLEMENTED")
    assert world.one("SELECT count(*) FROM evaluations") == 0
    assert world.one("SELECT count(*) FROM tool_calls") == 0  # the agent never ran


def test_a_manifest_needing_an_unimplemented_effect_is_refused_not_ignored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = submit_run(world.store, scenario="invoice-429")
    manifest = world.repo.manifest(run_id)
    supervisor = world.worker().supervisor
    assert supervisor._unsupported(manifest) is None
    # Every effect is implemented now; pretend one is not to prove the guard still fails closed.
    monkeypatch.setattr("agentcrash.workers.supervisor.IMPLEMENTED_EFFECTS", frozenset({"delay"}))
    assert supervisor._unsupported(manifest) == ("FAULTS_NOT_IMPLEMENTED",)


# --- cancellation -------------------------------------------------------------------------


def test_cancel_before_start_completes_without_an_attempt_and_is_idempotent(world: World) -> None:
    run_id = submit_run(world.store)
    client = world.api()
    first = client.post(
        f"/api/v1/runs/{run_id}/cancel", json={"reason": "no longer needed"}, headers=AUTH
    )
    assert first.status_code == 202
    assert (first.json()["status"], first.json()["verdict"]) == ("completed", None)
    again = client.post(f"/api/v1/runs/{run_id}/cancel", headers=AUTH)
    assert again.status_code == 200 and again.json()["status"] == "completed"
    assert world.worker().queue.claim_execute("w1") is None
    assert world.attempts(run_id) == [] and world.one("SELECT count(*) FROM evaluations") == 0
    assert client.post(f"/api/v1/runs/{uuid4()}/cancel", headers=AUTH).status_code == 404
    bad = client.post(f"/api/v1/runs/{run_id}/cancel", json={"nope": 1}, headers=AUTH)
    assert bad.status_code == 422


def _cancel_when(tool: str, world: World, run_id: UUID, *, after: bool) -> Hooks:
    """Hook that requests cancellation immediately before/after ``tool`` is executed."""

    def fire(request: Any, response: Any = None) -> None:
        if request.tool == tool:
            assert world.store.cancel_run(run_id, "test") is not None

    return Hooks(before_call=None if after else fire, after_call=fire if after else None)


def test_cancel_before_the_write_commits_nothing_and_is_inconclusive(world: World) -> None:
    run_id = submit_run(world.store)
    world.drain(world.worker(hooks=_cancel_when("ledger.post_invoice", world, run_id, after=False)))
    run = world.run(run_id)
    assert (run.status, run.verdict) == ("completed", "inconclusive")
    (attempt,) = world.attempts(run_id)
    assert attempt.termination == "cancelled"
    assert attempt.final_state["ledger_entries"] == []
    assert "environment.mutated" not in event_types(world, attempt.id)
    assert event_types(world, attempt.id)[-1] == "attempt.closed"


def test_cancel_after_the_write_keeps_the_committed_state_and_audits_it(world: World) -> None:
    run_id = submit_run(world.store)
    world.drain(world.worker(hooks=_cancel_when("ledger.post_invoice", world, run_id, after=True)))
    run = world.run(run_id)
    assert (run.status, run.verdict) == ("completed", "inconclusive")  # never a pass
    (attempt,) = world.attempts(run_id)
    assert attempt.termination == "cancelled"
    assert len(attempt.final_state["ledger_entries"]) == 1  # committed before the cancel: kept
    types = event_types(world, attempt.id)
    assert "environment.mutated" in types and types[-1] == "attempt.closed"
    grades = {
        g["grader_id"]: g["result"]
        for g in world.one("SELECT grades FROM evaluations WHERE attempt_id = :a", a=attempt.id)
    }
    assert grades["invoice.no_duplicate.v1"] == "pass"  # partial safety findings are still shown


# --- deadline -----------------------------------------------------------------------------


def test_a_hung_call_hits_the_real_deadline_and_the_child_is_stopped(world: World) -> None:
    release = threading.Event()
    seen: list[Any] = []

    def block(request: Any) -> None:
        seen.append(request.tool)
        release.wait(timeout=30)

    run_id = submit_run(world.store, budgets={"wall_ms": 1500})
    worker = world.worker(hooks=Hooks(before_call=block))
    child_holder: list[Any] = []
    original = worker.supervisor._spawn

    def spawn(spec: dict[str, Any], workdir: str) -> Any:
        child = original(spec, workdir)
        child_holder.append(child)
        return child

    worker.supervisor._spawn = spawn  # type: ignore[method-assign]
    try:
        world.drain(worker)
    finally:
        release.set()
    assert seen == ["invoice.get"]
    assert child_holder[0].poll() is not None  # no orphaned child
    run = world.run(run_id)
    (attempt,) = world.attempts(run_id)
    assert attempt.termination == "deadline_exceeded"
    assert (run.status, run.verdict) == ("completed", "fail")  # budget overrun fails, never passes
    grades = {
        g["grader_id"]: g
        for g in world.one("SELECT grades FROM evaluations WHERE attempt_id = :a", a=attempt.id)
    }
    assert grades["budget.within_limits.v1"]["reason_code"] == "BUDGET_EXCEEDED"


def test_the_tool_call_budget_terminates_the_attempt(world: World) -> None:
    run_id = submit_run(world.store, budgets={"tool_calls": 2})
    world.drain(world.worker())
    (attempt,) = world.attempts(run_id)
    assert attempt.termination == "budget_exceeded"
    run = world.run(run_id)
    assert (run.status, run.verdict) == ("completed", "fail")
    assert event_types(world, attempt.id).count("tool.requested") == 2


# --- infrastructure failure and recovery ---------------------------------------------------


def test_child_killed_after_the_write_commits_is_replaced_from_a_fresh_fixture(
    world: World,
) -> None:
    run_id = submit_run(world.store)
    killed: list[bool] = []
    worker = world.worker()

    def kill_once(request: Any, response: Any) -> None:
        if request.tool == "ledger.post_invoice" and not killed:
            killed.append(True)
            assert worker.supervisor.child is not None
            worker.supervisor.child.kill()  # the write has committed; the agent never learns

    worker.supervisor.hooks.after_call = kill_once
    world.drain(worker, limit=20)

    first, second = world.attempts(run_id)
    assert first.termination == "worker_lost"
    assert "environment.mutated" in event_types(world, first.id)  # the commit really happened
    assert second.termination == "agent_completed"
    assert second.id != first.id and second.fencing_token > first.fencing_token
    run = world.run(run_id)
    assert (run.status, run.verdict, run.selected_attempt_id) == ("completed", "pass", second.id)
    # The replacement started from the fixture, not from the lost attempt's world.
    assert second.initial_state["ledger_entries"] == [] and second.initial_state["receipts"] == []
    assert len(second.final_state["ledger_entries"]) == 1
    assert world.one("SELECT count(*) FROM ledger_entries") == 2  # one per attempt namespace
    with (
        pytest.raises(StaleFence),  # the lost attempt can never commit again
        world.repo.gate(first.id, first.fencing_token).transaction(),
    ):
        pass


def test_a_suspended_worker_that_wakes_up_after_reassignment_is_fenced(world: World) -> None:
    run_id = submit_run(world.store)
    gate, resume = threading.Event(), threading.Event()

    def suspend(request: Any) -> None:
        gate.set()
        resume.wait(timeout=60)

    slow = world.worker("slow", hooks=Hooks(before_call=suspend))
    outcome: list[str] = []
    claim = slow.queue.claim_execute("slow")
    assert claim is not None
    thread = threading.Thread(target=lambda: outcome.append(slow.supervisor.run_attempt(claim)))
    thread.start()
    try:
        assert gate.wait(timeout=60)  # the old worker is mid-call, holding its lease
        with world.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET lease_until = now() - interval '1 second'"))
        assert len(world.queue.reap_expired()) == 1
        world.drain(world.worker("fast"))  # the replacement runs to completion
    finally:
        resume.set()  # the old worker wakes up now
        thread.join(timeout=60)

    assert outcome == ["fenced"]  # it published nothing after losing the run
    first, second = world.attempts(run_id)
    assert first.termination == "worker_lost" and second.termination == "agent_completed"
    # Only what committed before it was suspended; nothing leaked in after the fence moved.
    assert event_types(world, first.id) == ["attempt.started", "agent.step"]
    assert world.one("SELECT count(*) FROM ledger_entries WHERE attempt_id = :a", a=first.id) == 0
    run = world.run(run_id)
    assert (run.status, run.verdict, run.selected_attempt_id) == ("completed", "pass", second.id)


def test_two_lost_attempts_exhaust_the_run_with_infra_worker_lost(world: World) -> None:
    run_id = submit_run(world.store)
    worker = world.worker()

    def kill_every_time(request: Any, response: Any) -> None:
        if request.tool == "invoice.get":
            assert worker.supervisor.child is not None
            worker.supervisor.child.kill()

    worker.supervisor.hooks.after_call = kill_every_time
    world.drain(worker, limit=20)
    run = world.run(run_id)
    assert (run.status, run.verdict, run.error_code) == ("error", None, "INFRA_WORKER_LOST")
    assert [a.termination for a in world.attempts(run_id)] == ["worker_lost", "worker_lost"]
    assert world.one("SELECT count(*) FROM evaluations") == 0  # lost evidence is never graded


def test_a_draining_worker_abandons_its_attempt_for_recovery(world: World) -> None:
    run_id = submit_run(world.store)
    worker = world.worker(config=replace(FAST, drain_s=0.0))
    entered = threading.Event()

    def stall_and_shutdown(request: Any) -> None:
        entered.set()
        worker.supervisor.shutdown.set()
        threading.Event().wait(2)  # hold the call so the drain window passes

    worker.supervisor.hooks.before_call = stall_and_shutdown
    claim = worker.queue.claim_execute("w1")
    assert claim is not None
    assert worker.supervisor.run_attempt(claim) == "abandoned"
    (first,) = world.attempts(run_id)
    assert first.termination == "worker_lost"
    assert world.one("SELECT count(*) FROM jobs WHERE status = 'queued' AND kind = 'execute'") == 1


# --- duplicate delivery -------------------------------------------------------------------


def test_duplicate_grading_publishes_one_evaluation(world: World) -> None:
    run_id = submit_run(world.store)
    worker = world.worker()
    world.drain(worker)
    (attempt,) = world.attempts(run_id)
    claim_like = world.queue.claim_grade("w1")
    assert claim_like is None  # the job was consumed
    evaluation = world.one("SELECT verdict FROM evaluations")
    from agentcrash.domain.ports import GradeClaim

    again = GradeClaim(uuid4(), run_id, attempt.id)  # a re-delivered job for the same attempt
    worker.grade(again)
    worker.grade(again)
    assert world.one("SELECT count(*) FROM evaluations") == 1
    assert world.one("SELECT verdict FROM evaluations") == evaluation == "pass"
    assert world.one("SELECT count(*) FROM run_attempts") == 1  # nothing was re-executed


def test_a_finished_run_is_never_executed_twice(world: World) -> None:
    submit_run(world.store)
    world.drain(world.worker())
    assert world.queue.claim_execute("w2") is None
    assert world.one("SELECT count(*) FROM run_attempts") == 1


# --- evidence is append-only --------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET payload = '{}'::jsonb",
        "DELETE FROM events",
        "UPDATE command_receipts SET result = '{}'::jsonb",
        "DELETE FROM command_receipts",
    ],
)
def test_events_and_receipts_cannot_be_rewritten(world: World, statement: str) -> None:
    submit_run(world.store)
    world.drain(world.worker())
    with pytest.raises(Exception, match="immutable"), world.engine.begin() as conn:
        conn.execute(text(statement))
