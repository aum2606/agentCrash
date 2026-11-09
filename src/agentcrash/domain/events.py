"""Authoritative event records and the hash chain (CONTRACTS.md section 7).

``H0 = SHA256("agentcrash-events-v1")`` and ``Hi = SHA256(bytes.fromhex(H(i-1)) ||
canonical(projected_event_i))``. The projection keeps only the observable fields, so wall
time, storage UUIDs and trace IDs never affect the chain. The chain detects accidental damage;
anyone who controls the database can rewrite it, so it is not tamper-proof attestation.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from agentcrash.domain.canonical import canonical_bytes

EVENT_TYPES: Final = (
    "attempt.started",
    "agent.step",
    "tool.requested",
    "policy.decided",
    "fault.decided",
    "environment.mutated",
    "tool.completed",
    "tool.transport_failed",
    "clock.advanced",
    "agent.finished",
    "attempt.closed",
)
GENESIS: Final = hashlib.sha256(b"agentcrash-events-v1").hexdigest()
MAX_EVENT_BYTES: Final = 128 * 1024


class EventTooLarge(ValueError):
    """An event payload exceeds the 128 KiB per-event cap (no silent truncation)."""


@dataclass(frozen=True)
class PendingEvent:
    type: str
    logical_time_ms: int
    actor_id: str | None
    call_id: str | None
    payload: dict[str, Any]
    wall_time: datetime


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    type: str
    logical_time_ms: int
    actor_id: str | None
    call_id: str | None
    payload: dict[str, Any]
    wall_time: datetime
    prev_hash: str
    event_hash: str


def project(
    seq: int,
    type_: str,
    logical_time_ms: int,
    actor_id: str | None,
    call_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "type": type_,
        "logical_time_ms": logical_time_ms,
        "actor_id": actor_id,
        "call_id": call_id,
        "payload": payload,
    }


def next_hash(prev_hash: str, projected: dict[str, Any]) -> str:
    body = canonical_bytes(projected)
    if len(body) > MAX_EVENT_BYTES:
        raise EventTooLarge
    return hashlib.sha256(bytes.fromhex(prev_hash) + body).hexdigest()


def chain_problem(events: Sequence[StoredEvent]) -> str | None:
    """First integrity problem in an attempt's events, or None when the chain is intact:
    contiguous ``seq`` from 1, each ``prev_hash`` and ``event_hash`` recomputed."""
    prev = GENESIS
    for i, ev in enumerate(events, start=1):
        if ev.seq != i:
            return f"EVENT_GAP at position {i} (seq {ev.seq})"
        if ev.prev_hash != prev:
            return f"BROKEN_LINK at seq {ev.seq}"
        projected = project(
            ev.seq, ev.type, ev.logical_time_ms, ev.actor_id, ev.call_id, ev.payload
        )
        try:
            expected = next_hash(prev, projected)
        except EventTooLarge:
            return f"EVENT_TOO_LARGE at seq {ev.seq}"
        if ev.event_hash != expected:
            return f"HASH_MISMATCH at seq {ev.seq}"
        prev = ev.event_hash
    return None


def observable_digest(events: Iterable[StoredEvent]) -> str:
    """Final chain head over the projected events (the canonical observable digest)."""
    head = GENESIS
    for ev in events:
        head = next_hash(
            head, project(ev.seq, ev.type, ev.logical_time_ms, ev.actor_id, ev.call_id, ev.payload)
        )
    return head
