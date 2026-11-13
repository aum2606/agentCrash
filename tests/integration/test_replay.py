"""Strict replay of real recorded attempts (real worker, child process, PostgreSQL): every
reference-agent fixture replays with the tool dispatcher and the network forbidden, source rows
never change, and the realistic ways a replay can go wrong are reported as divergences."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from agentcrash.agents.registry import ADAPTERS
from agentcrash.agents.rng import SeededRng
from agentcrash.domain.models import Manifest
from agentcrash.domain.task import task_from_manifest
from agentcrash.replay.bundle import build_bundle, read_bundle, write_bundle
from agentcrash.replay.engine import (
    Item,
    NotReplayable,
    build_transcript,
    strict_replay,
)
from agentcrash.replay.service import Incompatible, check_replayable, replay_recorded
from agentcrash.storage.store import PostgresStore

from .helpers import DATA_TABLES, submit_run
from .test_execution import World

CASES = [
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
AGENTS = ["invoice-naive", "invoice-resilient"]
TABLES = [
    "runs",
    "run_attempts",
    "events",
    "tool_calls",
    "command_receipts",
    "ledger_entries",
    "evaluations",
    "jobs",
]


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


def record(
    world: World, scenario: str, agent: str, **budgets: int
) -> tuple[UUID, Any, Manifest, Any]:
    run_id = submit_run(world.store, scenario=scenario, agent=agent, budgets=budgets or None)
    world.drain(world.worker())
    (attempt,) = world.attempts(run_id)
    manifest = world.repo.manifest(run_id)
    evidence = world.repo.evidence(attempt.id, manifest.budgets, {})
    return run_id, attempt, manifest, evidence


def fingerprint(world: World) -> dict[str, Any]:
    """Row counts and content digests of every table replay must not touch."""
    out: dict[str, Any] = {}
    with world.engine.connect() as conn:
        for table in TABLES:
            sql = f"SELECT count(*), md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t"  # noqa: S608, E501 - fixed names
            out[table] = conn.execute(text(sql)).one()
    return out


class Forbidden(AssertionError):
    pass


def forbid_effects(m: pytest.MonkeyPatch) -> None:
    """Replace the tool dispatcher and the gateway with tripwires and forbid every connection
    except the event loop's own internal socketpair (Windows' loop opens one at startup)."""
    real_connect = socket.socket.connect

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise Forbidden("replay reached a real effect")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        internal = {"socketpair", "_fallback_socketpair", "_make_self_pipe"}
        if any(frame.name in internal for frame in traceback.extract_stack()):
            return real_connect(self, address)
        raise Forbidden(f"replay tried to connect to {address!r}")

    m.setattr("agentcrash.environments.invoice.tools.dispatch", boom)
    m.setattr("agentcrash.execution.gateway.Gateway.tool_call", boom)
    m.setattr(socket.socket, "connect", guarded_connect)
    m.setattr(socket, "create_connection", boom)


def replay(manifest: Manifest, evidence: Any) -> Any:
    return replay_recorded(manifest, evidence.events, evidence.tool_calls, ADAPTERS, SeededRng)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("scenario", CASES)
def test_every_reference_fixture_replays_strictly_without_effects_or_source_changes(
    world: World, scenario: str, agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, manifest, evidence = record(world, scenario, agent)  # a normal, live recording
    before = fingerprint(world)
    with monkeypatch.context() as forbidden:
        forbid_effects(forbidden)  # only the replay itself runs under the tripwires
        result = replay(manifest, evidence)
    assert result.ok, result.divergence
    assert result.consumed == result.total > 1  # every recorded item was consumed
    assert fingerprint(world) == before  # no source run data changed


def test_a_different_seed_changes_the_jitter_and_is_reported_at_the_wait(world: World) -> None:
    _, _, manifest, evidence = record(world, "invoice-429", "invoice-resilient")
    transcript = build_transcript(evidence.events, evidence.tool_calls)
    waits = [i for i in transcript if i.kind == "sleep"]
    assert len(waits) == 1
    divergence = None
    for seed in ("9999", "1", "2", "3"):  # a different jitter value shows up as a changed wait
        result = replay(manifest.model_copy(update={"seed": seed}), evidence)
        divergence = result.divergence
        if divergence is not None:
            break
    assert divergence is not None and divergence.kind == "sleep_changed"
    assert (
        divergence.expected
        and divergence.actual
        and divergence.expected["hash"] != divergence.actual["hash"]
    )


def test_replaying_with_a_different_adapter_fails_clearly_at_the_first_difference(
    world: World,
) -> None:
    # On the healthy path both agents behave identically (a legitimate match), so use a fault the
    # naive agent does not handle: it stops where the resilient one waits and retries.
    _, _, manifest, evidence = record(world, "invoice-429", "invoice-resilient")
    transcript = build_transcript(evidence.events, evidence.tool_calls)
    naive = ADAPTERS[("invoice-naive", "1.0.0")]
    result = asyncio.run(
        strict_replay(
            naive,
            SeededRng(manifest.seed, "invoice-naive"),
            task_from_manifest(manifest),
            transcript,
        )
    )
    assert not result.ok and result.divergence is not None
    assert result.divergence.code == "REPLAY_DIVERGENCE"
    assert result.divergence.kind == "missing_events"  # naive gave up where resilient waited


def tampered(world: World, scenario: str, edit: Any) -> Any:
    _, _, manifest, evidence = record(world, scenario, "invoice-resilient")
    transcript = edit(build_transcript(evidence.events, evidence.tool_calls))
    adapter = ADAPTERS[(manifest.agent.id, manifest.agent.version)]
    return asyncio.run(
        strict_replay(
            adapter,
            SeededRng(manifest.seed, manifest.agent.id),
            task_from_manifest(manifest),
            transcript,
        )
    )


def first_call(items: list[Item]) -> int:
    return next(i for i, item in enumerate(items) if item.kind == "call")


def test_a_recording_with_a_changed_argument_fails_at_that_call(world: World) -> None:
    def edit(items: list[Item]) -> list[Item]:
        i = first_call(items)
        items[i] = replace(items[i], data={**items[i].data, "args_hash": "0" * 64})
        return items

    d = tampered(world, "invoice-healthy", edit).divergence
    assert d is not None and d.kind == "call_changed" and "args_hash" in d.detail


def test_a_recording_missing_a_call_makes_the_agents_next_call_unexpected(world: World) -> None:
    def edit(items: list[Item]) -> list[Item]:
        del items[first_call(items)]
        return items

    d = tampered(world, "invoice-healthy", edit).divergence
    assert d is not None and d.kind in ("unexpected_call", "call_changed", "reordered_call")


def test_a_recording_with_an_extra_call_leaves_it_unconsumed(world: World) -> None:
    def edit(items: list[Item]) -> list[Item]:
        items.insert(
            len(items) - 1,
            replace(
                items[first_call(items)],
                data={**items[first_call(items)].data, "call_id": "call-99"},
            ),
        )
        return items

    d = tampered(world, "invoice-healthy", edit).divergence
    assert d is not None and d.kind == "missing_events"


def test_a_recording_with_reordered_calls_is_named_as_reordered(world: World) -> None:
    def edit(items: list[Item]) -> list[Item]:
        idx = [i for i, item in enumerate(items) if item.kind == "call"]
        items[idx[0]], items[idx[1]] = items[idx[1]], items[idx[0]]
        return items

    d = tampered(world, "invoice-healthy", edit).divergence
    assert d is not None and d.kind == "reordered_call"


def test_a_changed_recorded_terminal_result_fails(world: World) -> None:
    def edit(items: list[Item]) -> list[Item]:
        items[-1] = Item("finish", {"status": "gave_up", "summary": "edited"})
        return items

    d = tampered(world, "invoice-healthy", edit).divergence
    assert d is not None and d.kind == "terminal_result_changed"


def test_incompatible_recordings_are_refused_before_anything_runs(world: World) -> None:
    _, _, manifest, evidence = record(world, "invoice-healthy", "invoice-naive")
    assert check_replayable(manifest, ADAPTERS) == []
    cases = {
        "REPLAY_CAPABILITY_PARTIAL": {"replay_capability": "partial"},
        "REPLAY_CAPABILITY_NONE": {"replay_capability": "none"},
        "VERSION_INCOMPATIBLE_FAULT_ENGINE": {"fault_engine_version": "0"},
        "VERSION_INCOMPATIBLE_TAXONOMY": {"taxonomy_version": "0"},
        "VERSION_INCOMPATIBLE_CANONICALIZATION": {"canonicalization_version": "acl-c14n-v0"},
    }
    for code, change in cases.items():
        with pytest.raises(Incompatible) as err:
            replay(manifest.model_copy(update=change), evidence)
        assert code in err.value.reasons
    old_agent = manifest.model_copy(
        update={"agent": manifest.agent.model_copy(update={"version": "0.9.0"})}
    )
    with pytest.raises(Incompatible) as err:
        replay(old_agent, evidence)
    assert err.value.reasons == ["ADAPTER_VERSION_UNAVAILABLE"]


def test_an_attempt_without_a_terminal_result_cannot_be_replayed(world: World) -> None:
    _, attempt, manifest, evidence = record(
        world, "invoice-healthy", "invoice-resilient", tool_calls=2
    )
    assert attempt.termination == "budget_exceeded"
    with pytest.raises(NotReplayable):
        replay(manifest, evidence)


# --- from an exported bundle, with the source database gone -----------------------------------


def wipe(world: World) -> None:
    with world.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {DATA_TABLES} CASCADE"))


def export(world: World, scenario: str, agent: str, tmp_path: Path, **budgets: int) -> Path:
    _, attempt, manifest, evidence = record(world, scenario, agent, **budgets)
    with world.engine.connect() as conn:
        rows = conn.execute(text("SELECT verdict, primary_reason FROM evaluations")).all()
    data = build_bundle(
        manifest,
        evidence.events,
        evidence.tool_calls,
        [{"verdict": r.verdict, "primary_reason": r.primary_reason} for r in rows],
    )
    return write_bundle(tmp_path, data)


@pytest.mark.parametrize(
    ("scenario", "agent"),
    [
        ("invoice-healthy", "invoice-naive"),
        ("invoice-429", "invoice-resilient"),
        ("invoice-timeout-after", "invoice-resilient"),
        ("invoice-cas-conflict", "invoice-resilient"),
        ("invoice-document-injection", "invoice-naive"),
    ],
)
def test_an_exported_bundle_replays_strictly_after_the_source_database_is_emptied(
    world: World, scenario: str, agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = export(world, scenario, agent, tmp_path)
    wipe(world)  # nothing of the run remains in PostgreSQL
    assert fingerprint(world)["events"][0] == 0
    with monkeypatch.context() as forbidden:
        forbid_effects(forbidden)
        bundle = read_bundle(path)
        result = replay_recorded(bundle.manifest, bundle.events, bundle.calls, ADAPTERS, SeededRng)
    assert result.ok, result.divergence
    assert result.consumed == result.total > 1
    assert bundle.evaluations and bundle.bundle_sha256 == path.stem


def test_a_bundle_marked_partial_is_playable_but_refuses_strict_replay(
    world: World, tmp_path: Path
) -> None:
    _, _, manifest, evidence = record(world, "invoice-healthy", "invoice-naive")
    partial = manifest.model_copy(update={"replay_capability": "partial"})
    bundle = read_bundle(
        write_bundle(tmp_path, build_bundle(partial, evidence.events, evidence.tool_calls))
    )
    assert len(bundle.events) == len(evidence.events)  # playback evidence is all there
    with pytest.raises(Incompatible) as err:
        replay_recorded(bundle.manifest, bundle.events, bundle.calls, ADAPTERS, SeededRng)
    assert err.value.reasons == ["REPLAY_CAPABILITY_PARTIAL"]


def test_a_budget_stopped_attempt_exports_for_playback_but_cannot_be_replayed(
    world: World, tmp_path: Path
) -> None:
    path = export(world, "invoice-healthy", "invoice-resilient", tmp_path, tool_calls=2)
    bundle = read_bundle(path)
    assert any(e.type == "attempt.closed" for e in bundle.events)
    with pytest.raises(NotReplayable):
        replay_recorded(bundle.manifest, bundle.events, bundle.calls, ADAPTERS, SeededRng)


# --- the offline CLI on real exported bundles --------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "agent"),
    [
        ("invoice-429", "invoice-resilient"),
        ("invoice-cas-conflict", "invoice-resilient"),
        ("invoice-document-injection", "invoice-naive"),
    ],
)
def test_the_cli_plays_back_and_strictly_replays_an_exported_bundle_with_no_database(
    world: World, scenario: str, agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agentcrash.cli.main import app

    path = export(world, scenario, agent, tmp_path)
    wipe(world)
    cli = CliRunner()
    with monkeypatch.context() as forbidden:
        forbid_effects(forbidden)
        shown = cli.invoke(app, ["playback", "--bundle", str(path), "--json"])
        replayed = cli.invoke(app, ["replay", "--bundle", str(path), "--strict"])
    assert shown.exit_code == 0, shown.output
    report = json.loads(shown.stdout)
    assert report["run"]["scenario"]["id"] == scenario and report["evaluations"]
    assert replayed.exit_code == 0, replayed.output
    consumed, _, total = replayed.stdout.split("matched: ")[1].split()[0].partition("/")
    assert consumed == total and int(total) > 1


def test_a_real_bundle_replays_in_a_clean_process_with_an_unreachable_database(
    world: World, tmp_path: Path
) -> None:
    path = export(world, "invoice-429", "invoice-resilient", tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "AGENTCRASH_"))}
    env["AGENTCRASH_DATABASE_URL"] = "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none"
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-c",
            "from agentcrash.cli.main import app; app()",
            "replay",
            "--bundle",
            str(path),
            "--strict",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("matched: ")


def test_a_tampered_real_bundle_is_refused_by_the_cli(world: World, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agentcrash.cli.main import app

    path = export(world, "invoice-429", "invoice-resilient", tmp_path)
    data = bytearray(path.read_bytes())
    data[len(data) // 3] ^= 0x01
    bad = tmp_path / "tampered.acbundle"
    bad.write_bytes(bytes(data))
    for command in (["playback"], ["replay", "--strict"]):
        result = CliRunner().invoke(app, [command[0], "--bundle", str(bad), *command[1:]])
        assert result.exit_code == 2 and "error: BUNDLE_" in result.output
