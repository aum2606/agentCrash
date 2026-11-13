"""Strict replay (ENGINEERING_PLAN.md section 5, ADR-006).

A recorded attempt's observable agent interaction is rebuilt from its authoritative events and
stored call outcomes (the *transcript*). Replay re-runs a compatible, allowlisted adapter in
process against that transcript: every tool call, step and clock wait the agent makes must match
the next recorded item, and the recorded response (or timeout) is handed back. There is no
gateway, no database handle, no tool dispatcher and no network in this module, so a replay cannot
write anything. It verifies the agent's interaction with the recorded environment; it does not
re-validate the tool implementation (rerun tests cover that).

Replay stops at the first divergence and reports it with the sequence position and the hashes of
what was expected and what the agent did. All recorded items must be consumed and the terminal
agent result must match.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.events import StoredEvent
from agentcrash.domain.redaction import redact
from agentcrash.domain.runtime import (
    AgentAdapter,
    AgentResult,
    AgentRng,
    AgentTask,
    StoredCall,
    ToolRequest,
    ToolResponse,
    ToolTimeout,
)

NOTE_LIMIT = 200  # the gateway truncates step notes before recording them
Kind = Literal["call", "step", "sleep", "finish"]


class NotReplayable(Exception):
    """The recorded attempt cannot be replayed (for example it never produced a result)."""


# --- the recorded transcript ----------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    kind: Kind
    data: dict[str, Any]
    outcome: dict[str, Any] | None = None  # calls only: the stored ToolResponse / transport error

    def digest(self) -> str:
        return canonical_hash({"kind": self.kind, **self.data})


Transcript = list[Item]


def build_transcript(events: Sequence[StoredEvent], calls: Sequence[StoredCall]) -> Transcript:
    """The ordered agent-visible interaction of one attempt. Gateway-internal events (policy and
    fault decisions, mutations, completions, closure) are not agent actions and are skipped;
    ``clock.advanced`` events caused by faults carry a ``call_id`` and are skipped too."""
    outcomes = {c.call_id: c.outcome for c in calls}
    items: Transcript = []
    for e in events:
        p = e.payload
        if e.type == "agent.step":
            items.append(Item("step", {"note": p["note"]}))
        elif e.type == "tool.requested":
            if e.call_id not in outcomes:
                raise NotReplayable(f"call {e.call_id} has no stored outcome")
            items.append(
                Item(
                    "call",
                    {
                        "call_id": e.call_id,
                        "actor_id": e.actor_id,
                        "tool": p["tool"],
                        "args_hash": p["args_hash"],
                        "idempotency_key": p.get("idempotency_key"),
                    },
                    outcomes[e.call_id],
                )
            )
        elif e.type == "clock.advanced" and e.call_id is None:
            items.append(Item("sleep", {"duration_ms": p["duration_ms"], "to_ms": p["to_ms"]}))
        elif e.type == "agent.finished":
            items.append(Item("finish", {"status": p["status"], "summary": p["summary"]}))
    if not items or items[-1].kind != "finish":
        raise NotReplayable("the attempt recorded no terminal agent result")
    return items


# --- divergence -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """``REPLAY_DIVERGENCE``: the first place the replayed agent left the recording."""

    kind: str
    position: int  # index into the transcript
    detail: str
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    code: str = "REPLAY_DIVERGENCE"


class _Diverged(BaseException):
    """Raised inside the agent's call stack; a BaseException so no agent can swallow it."""

    def __init__(self, divergence: Divergence) -> None:
        super().__init__(divergence.kind)
        self.divergence = divergence


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    consumed: int
    total: int
    divergence: Divergence | None = None
    result: AgentResult | None = None


def _described(item: Item | None) -> dict[str, Any] | None:
    return None if item is None else {"kind": item.kind, "hash": item.digest(), **item.data}


class _Cursor:
    def __init__(self, items: Transcript) -> None:
        self.items = items
        self.pos = 0

    def peek(self) -> Item:
        return self.items[self.pos]

    def diverge(self, kind: str, detail: str, actual: Item | None) -> _Diverged:
        return _Diverged(
            Divergence(kind, self.pos, detail, _described(self.peek()), _described(actual))
        )

    def take(self, actual: Item) -> Item:
        """Consume the next item if it equals ``actual`` in kind and data, else diverge."""
        expected = self.peek()
        if expected.kind == "finish" and actual.kind != "finish":
            raise self.diverge(f"extra_{actual.kind}", "the recording had already ended", actual)
        if expected.kind != actual.kind:
            raise self.diverge(
                f"unexpected_{actual.kind}", f"the recording expected a {expected.kind}", actual
            )
        if expected.data != actual.data:
            if actual.kind == "call":
                later = [i for i in self.items[self.pos + 1 :] if i.kind == "call"]
                if any(i.data == actual.data for i in later):
                    raise self.diverge("reordered_call", "this call was recorded later", actual)
            changed = sorted(k for k in actual.data if expected.data.get(k) != actual.data[k])
            raise self.diverge(f"{actual.kind}_changed", f"fields differ: {changed}", actual)
        self.pos += 1
        return expected


class _ReplayTools:
    def __init__(self, cursor: _Cursor) -> None:
        self._c = cursor

    async def call(self, request: ToolRequest) -> ToolResponse:
        view = redact(request.args)
        recorded = self._c.take(
            Item(
                "call",
                {
                    "call_id": request.call_id,
                    "actor_id": request.actor_id,
                    "tool": request.tool,
                    "args_hash": canonical_hash(view),
                    "idempotency_key": redact(request.idempotency_key),
                },
            )
        )
        outcome = recorded.outcome or {}
        if outcome.get("kind") == "transport_error":
            error = outcome["error"]
            raise ToolTimeout(error["stage"], error["elapsed_ms"])
        return ToolResponse.model_validate(outcome)

    async def step(self, note: str) -> None:
        self._c.take(Item("step", {"note": redact(note[:NOTE_LIMIT])}))


class _ReplayClock:
    """Mirrors the live ``GatewayClock``: logical time moves only through recorded waits."""

    def __init__(self, cursor: _Cursor) -> None:
        self._c = cursor
        self._now = 0

    def logical_now_ms(self) -> int:
        return self._now

    async def sleep_ms(self, duration_ms: int) -> None:
        item = self._c.peek()
        if item.kind == "sleep" and item.data["duration_ms"] == duration_ms:
            self._c.pos += 1
            self._now = int(item.data["to_ms"])
            return
        self._c.take(Item("sleep", {"duration_ms": duration_ms, "to_ms": self._now + duration_ms}))


async def strict_replay(
    factory: Callable[[], AgentAdapter],
    rng: AgentRng,
    task: AgentTask,
    transcript: Transcript,
) -> ReplayResult:
    """Run the adapter against ``transcript``. ``rng`` must be the seeded agent RNG the original
    run used (same seed and agent id), so retry jitter is reproduced rather than recorded."""
    cursor = _Cursor(transcript)
    total = len(transcript)
    try:
        try:
            result = await factory().run(task, _ReplayTools(cursor), _ReplayClock(cursor), rng)
        except Exception as err:  # mirrors the child runner's mapping of agent crashes
            result = AgentResult(status="error", summary=f"agent raised {type(err).__name__}")
        if cursor.peek().kind != "finish":
            raise cursor.diverge(
                "missing_events", "the agent finished before the recording did", None
            )
        actual = Item("finish", {"status": result.status, "summary": redact(result.summary)})
        if cursor.peek().data != actual.data:
            raise cursor.diverge("terminal_result_changed", "the agent result differs", actual)
        cursor.pos += 1
    except _Diverged as stop:
        return ReplayResult(False, cursor.pos, total, stop.divergence)
    return ReplayResult(True, cursor.pos, total, None, result)
