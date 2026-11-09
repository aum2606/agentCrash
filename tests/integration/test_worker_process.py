"""The real ``agentcrash worker`` process, killed after a write has committed.

Determinism without sleeps: a test-only trigger in the throwaway database makes the insert of the
``agent.finished`` event wait on an advisory lock that the test holds. The agent's write has
already committed by then, so the worker is parked at exactly "after the commit, before it can
finish". The test kills the whole worker process tree there, then lets a second worker recover.
"""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import URL, Engine, create_engine, text

from .helpers import ROOT, submit_run
from .test_execution import World

LOCK_KEY = 777001
WORKER = [sys.executable, "-c", "from agentcrash.cli.main import app; app()", "worker"]
TRIGGER_FN = f"""
CREATE FUNCTION test_block_finish() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_advisory_xact_lock({LOCK_KEY}); RETURN NEW; END $$
"""
TRIGGER = """
CREATE TRIGGER test_block_finish BEFORE INSERT ON events
FOR EACH ROW WHEN (NEW.type = 'agent.finished') EXECUTE FUNCTION test_block_finish()
"""


@pytest.fixture
def world(clean_engine: Engine, store: Any) -> World:
    return World(clean_engine, store)


def wait_for(condition: Callable[[], bool], what: str, timeout: float = 90.0) -> None:
    """Poll a database condition (not a duration) until it holds."""
    deadline = time.monotonic() + timeout
    gate = threading.Event()
    while time.monotonic() < deadline:
        if condition():
            return
        gate.wait(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def start_worker(database_url: URL, tmp_path: Any, name: str) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "AGENTCRASH_ROOT": str(ROOT),
        "AGENTCRASH_TOKEN_FILE": str(tmp_path / "token"),
        "AGENTCRASH_DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    return subprocess.Popen(  # noqa: S603
        WORKER,
        env=env,
        cwd=ROOT,
        stdout=(tmp_path / f"{name}.log").open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=sys.platform != "win32",
    )


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None or sys.platform == "win32":
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(proc.pid, 9)
    proc.wait(timeout=30)


@pytest.fixture
def parked_finish(clean_engine: Engine, database_url: URL) -> Iterator[Callable[[], None]]:
    """Hold the lock the trigger waits on; always clean up the trigger and the lock."""
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    holder = admin.connect()
    holder.execute(text(f"SELECT pg_advisory_lock({LOCK_KEY})"))
    with clean_engine.begin() as conn:
        conn.execute(text(TRIGGER_FN))
        conn.execute(text(TRIGGER))

    def release() -> None:
        holder.execute(text(f"SELECT pg_advisory_unlock({LOCK_KEY})"))

    try:
        yield release
    finally:
        release()
        holder.close()
        with clean_engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS test_block_finish ON events"))
            conn.execute(text("DROP FUNCTION IF EXISTS test_block_finish()"))
        admin.dispose()


def test_kill_dash_nine_of_the_worker_after_a_write_commit_is_recovered(
    world: World, database_url: URL, tmp_path: Any, parked_finish: Callable[[], None]
) -> None:
    run_id = submit_run(world.store)
    first = start_worker(database_url, tmp_path, "first")
    second: subprocess.Popen[bytes] | None = None
    try:
        # Parked exactly after the write committed: the mutation event exists and a backend is
        # waiting on the advisory lock that guards the agent's final event.
        wait_for(
            lambda: (
                bool(world.one("SELECT count(*) FROM events WHERE type = 'environment.mutated'"))
                and bool(
                    world.one(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    )
                )
            ),
            "the worker to commit the write and park",
        )
        kill_tree(first)  # SIGKILL / TerminateProcess of the worker and its child
        # The server cannot notice the dead client while its backend waits on the advisory
        # lock, and that backend holds a lock on ``events``. Releasing the advisory lock lets it
        # finish its statement, see the closed connection and roll back; nothing was committed.
        parked_finish()
        wait_for(
            lambda: (
                not world.one(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                )
            ),
            "the killed worker's blocked backend to be gone",
        )
        assert world.run(run_id).status == "running"  # nothing closed the attempt: it was just lost

        with world.engine.begin() as conn:  # the dead worker's lease runs out
            conn.execute(
                text(
                    "UPDATE jobs SET lease_until = now() - interval '1 second' "
                    "WHERE status = 'leased'"
                )
            )
        second = start_worker(database_url, tmp_path, "second")
        wait_for(lambda: world.run(run_id).status == "completed", "the replacement to finish")
    finally:
        for proc in (first, second):
            if proc is not None:
                kill_tree(proc)
        for name in ("first", "second"):  # worker logs, shown by pytest when the test fails
            log = tmp_path / f"{name}.log"
            if log.exists():
                tail = log.read_text(errors="replace")[-3000:]
                print(f"--- {name} worker log ---", tail, sep=chr(10))

    run = world.run(run_id)
    lost, replacement = world.attempts(run_id)
    assert lost.termination == "worker_lost" and replacement.termination == "agent_completed"
    assert (run.verdict, run.selected_attempt_id) == ("pass", replacement.id)
    lost_types = [e.type for e in world.events(lost.id)]
    assert "environment.mutated" in lost_types  # the write really committed before the kill
    assert "agent.finished" not in lost_types and "attempt.closed" not in lost_types
    assert replacement.initial_state["ledger_entries"] == []  # fresh fixture, not the lost world
    assert len(replacement.final_state["ledger_entries"]) == 1
    assert world.one("SELECT count(*) FROM evaluations") == 1  # only the replacement is graded
