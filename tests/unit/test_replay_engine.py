"""Strict replay core: exact match passes; every kind of departure is reported at the first
divergence with its position and hashes. Hand-built transcripts, scripted agents, no database."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.redaction import redact
from agentcrash.domain.runtime import (
    AgentAdapter,
    AgentResult,
    AgentRng,
    AgentTask,
    Clock,
    ToolClient,
    ToolRequest,
    ToolTimeout,
)
from agentcrash.replay.engine import Item, ReplayResult, Transcript, strict_replay

TASK = AgentTask(
    task_id="t",
    instruction="i",
    invoice_id="INV-1001",
    max_source_age_ms=1,
    logical_epoch="2026-01-01T00:00:00Z",
    seed="1",
)
OK = {
    "kind": "response",
    "status": 200,
    "body": {"x": 1},
    "meta": {"call_index": 1, "tool_ordinal": 1, "logical_time_ms": 0},
}


class FixedRng:
    def jitter_ms(self, upper_ms: int) -> int:
        return 0


def call(
    n: int, tool: str, args: dict[str, Any], key: str | None = None, outcome: Any = OK
) -> Item:
    return Item(
        "call",
        {
            "call_id": f"call-{n}",
            "actor_id": "agent",
            "tool": tool,
            "args_hash": canonical_hash(redact(args)),
            "idempotency_key": key,
        },
        outcome,
    )


def step(note: str) -> Item:
    return Item("step", {"note": note})


def sleep(duration: int, to: int) -> Item:
    return Item("sleep", {"duration_ms": duration, "to_ms": to})


def finish(status: str = "completed", summary: str = "done") -> Item:
    return Item("finish", {"status": status, "summary": summary})


Script = Callable[[ToolClient, Clock], Awaitable[AgentResult]]


class Scripted:
    def __init__(self, script: Script) -> None:
        self.script = script

    async def run(
        self, task: AgentTask, tools: ToolClient, clock: Clock, rng: AgentRng
    ) -> AgentResult:
        return await self.script(tools, clock)


def req(n: int, tool: str, args: dict[str, Any], key: str | None = None) -> ToolRequest:
    return ToolRequest(
        call_id=f"call-{n}", actor_id="agent", tool=tool, args=args, idempotency_key=key
    )


def replay(script: Script, transcript: Transcript) -> ReplayResult:
    def factory() -> AgentAdapter:
        return Scripted(script)

    return asyncio.run(strict_replay(factory, FixedRng(), TASK, transcript))


async def good(tools: ToolClient, clock: Clock) -> AgentResult:
    await tools.step("read")
    await tools.call(req(1, "invoice.get", {"invoice_id": "INV-1001"}))
    await clock.sleep_ms(250)
    await tools.call(req(2, "ledger.post_invoice", {"a": 1}, "post:INV-1001"))
    return AgentResult(status="completed", summary="done")


GOOD: Transcript = [
    step("read"),
    call(1, "invoice.get", {"invoice_id": "INV-1001"}),
    sleep(250, 250),
    call(2, "ledger.post_invoice", {"a": 1}, "post:INV-1001"),
    finish(),
]


def test_an_exact_match_consumes_everything_and_succeeds() -> None:
    result = replay(good, GOOD)
    assert result.ok and result.divergence is None
    assert (result.consumed, result.total) == (5, 5)


def test_changed_arguments_diverge_at_that_call_with_both_hashes() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.step("read")
        await tools.call(req(1, "invoice.get", {"invoice_id": "INV-9999"}))
        return AgentResult(status="completed", summary="done")

    d = replay(script, GOOD).divergence
    assert d is not None and d.code == "REPLAY_DIVERGENCE"
    assert (d.kind, d.position) == ("call_changed", 1)
    assert "args_hash" in d.detail
    assert d.expected and d.actual and d.expected["hash"] != d.actual["hash"]


@pytest.mark.parametrize(
    ("field", "make"),
    [
        ("idempotency_key", lambda: req(2, "ledger.post_invoice", {"a": 1}, "post:OTHER")),
        ("call_id", lambda: req(7, "ledger.post_invoice", {"a": 1}, "post:INV-1001")),
        ("tool", lambda: req(2, "receipt.get", {"a": 1}, "post:INV-1001")),
    ],
)
def test_a_changed_key_call_id_or_tool_is_a_changed_call(
    field: str, make: Callable[[], ToolRequest]
) -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.step("read")
        await tools.call(req(1, "invoice.get", {"invoice_id": "INV-1001"}))
        await clock.sleep_ms(250)
        await tools.call(make())
        return AgentResult(status="completed", summary="done")

    d = replay(script, GOOD).divergence
    assert d is not None and d.kind == "call_changed" and field in d.detail and d.position == 3


def test_an_extra_call_after_the_recording_ended_is_reported() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await good(tools, clock)
        await tools.call(req(3, "invoice.get", {"invoice_id": "INV-1001"}))
        return AgentResult(status="completed", summary="done")

    d = replay(script, GOOD).divergence
    assert d is not None and (d.kind, d.position) == ("extra_call", 4)


def test_a_missing_call_is_reported_when_the_agent_finishes_early() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.step("read")
        await tools.call(req(1, "invoice.get", {"invoice_id": "INV-1001"}))
        return AgentResult(status="completed", summary="done")

    d = replay(script, GOOD).divergence
    assert d is not None and (d.kind, d.position) == ("missing_events", 2)
    assert d.expected and d.expected["kind"] == "sleep" and d.actual is None


def test_a_reordered_call_is_named_as_such() -> None:
    transcript: Transcript = [
        call(1, "invoice.get", {"i": 1}),
        call(2, "purchase_order.get", {"p": 1}),
        finish(),
    ]

    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.call(req(2, "purchase_order.get", {"p": 1}))
        await tools.call(req(1, "invoice.get", {"i": 1}))
        return AgentResult(status="completed", summary="done")

    d = replay(script, transcript).divergence
    assert d is not None and (d.kind, d.position) == ("reordered_call", 0)


def test_the_wrong_kind_of_action_is_unexpected() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await clock.sleep_ms(10)
        return AgentResult(status="completed", summary="done")

    d = replay(script, GOOD).divergence
    assert d is not None and (d.kind, d.position) == ("unexpected_sleep", 0)


def test_changed_step_note_and_sleep_duration_are_reported() -> None:
    async def note(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.step("something else")
        return AgentResult(status="completed", summary="done")

    async def wait(tools: ToolClient, clock: Clock) -> AgentResult:
        await tools.step("read")
        await tools.call(req(1, "invoice.get", {"invoice_id": "INV-1001"}))
        await clock.sleep_ms(999)
        return AgentResult(status="completed", summary="done")

    assert (replay(note, GOOD).divergence or None) and replay(
        note, GOOD
    ).divergence.kind == "step_changed"  # type: ignore[union-attr]
    d = replay(wait, GOOD).divergence
    assert d is not None and (d.kind, d.position) == ("sleep_changed", 2)


def test_a_changed_terminal_result_fails_even_when_every_call_matched() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        await good(tools, clock)
        return AgentResult(status="gave_up", summary="nope")

    d = replay(script, GOOD).divergence
    assert d is not None and (d.kind, d.position) == ("terminal_result_changed", 4)


def test_a_recorded_timeout_is_raised_to_the_agent() -> None:
    timeout = {
        "kind": "transport_error",
        "error": {"code": "TOOL_TIMEOUT", "stage": "before_commit", "elapsed_ms": 7},
    }
    transcript: Transcript = [
        call(1, "invoice.get", {"i": 1}, outcome=timeout),
        finish("completed", "retried"),
    ]

    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        with pytest.raises(ToolTimeout) as err:
            await tools.call(req(1, "invoice.get", {"i": 1}))
        assert (err.value.stage, err.value.elapsed_ms) == ("before_commit", 7)
        return AgentResult(status="completed", summary="retried")

    assert replay(script, transcript).ok


def test_an_agent_cannot_swallow_a_divergence() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        try:
            await tools.call(req(1, "invoice.get", {"invoice_id": "WRONG"}))
        except Exception:
            return AgentResult(status="completed", summary="swallowed")
        return AgentResult(status="completed", summary="unreachable")

    d = replay(script, GOOD).divergence
    assert d is not None  # the divergence is a BaseException: `except Exception` does not catch it


def test_an_agent_crash_is_compared_like_the_live_child_runner_reports_it() -> None:
    async def script(tools: ToolClient, clock: Clock) -> AgentResult:
        raise KeyError("x")

    ok = replay(script, [finish("error", "agent raised KeyError")])
    assert ok.ok
    bad = replay(script, [finish("completed", "fine")])
    assert bad.divergence is not None and bad.divergence.kind == "terminal_result_changed"
