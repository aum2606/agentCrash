"""Execution contracts: what an agent sees, what the gateway stores, and the ports between them
(CONTRACTS.md sections 2 and 6). Agents get ``ToolClient``, ``Clock`` and ``AgentRng`` only: no
database handle, no manifest, no grader oracle.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

from pydantic import StringConstraints

from agentcrash.domain.events import PendingEvent, StoredEvent
from agentcrash.domain.models import Strict

SCHEMA_VERSION = "1.0"
MAX_ARGS_BYTES = 64 * 1024
CallId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]


class ToolRequest(Strict):
    call_id: CallId
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    tool: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    args: dict[str, Any] = {}
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class ToolMeta(Strict):
    call_index: int
    tool_ordinal: int
    logical_time_ms: int
    source_version: int | None = None
    schema_version: str = SCHEMA_VERSION


class ToolResponse(Strict):
    kind: Literal["response"] = "response"
    status: int
    body: dict[str, Any]
    meta: ToolMeta


class AgentTask(Strict):
    """Everything the agent is told. Hidden oracle values are not part of it."""

    task_id: str
    instruction: str
    invoice_id: str
    max_source_age_ms: int
    logical_epoch: str
    seed: str


class AgentResult(Strict):
    status: Literal["completed", "gave_up", "error"]
    summary: Annotated[str, StringConstraints(max_length=500)] = ""


class ToolTimeout(Exception):
    """Simulated transport failure (a real tool HTTP error is a ``ToolResponse``)."""

    def __init__(self, stage: str, elapsed_ms: int) -> None:
        super().__init__(f"tool timeout at {stage} after {elapsed_ms} ms")
        self.stage = stage
        self.elapsed_ms = elapsed_ms


class AttemptEnded(Exception):
    """The gateway refused further calls: cancelled, over budget, fenced out or deadline hit.
    The agent must stop; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ToolClient(Protocol):
    async def call(self, request: ToolRequest) -> ToolResponse: ...
    async def step(self, note: str) -> None: ...


class Clock(Protocol):
    def logical_now_ms(self) -> int: ...
    async def sleep_ms(self, duration_ms: int) -> None: ...


class AgentRng(Protocol):
    def jitter_ms(self, upper_ms: int) -> int: ...


class AgentAdapter(Protocol):
    async def run(
        self, task: AgentTask, tools: ToolClient, clock: Clock, rng: AgentRng
    ) -> AgentResult: ...


# --- storage-side ports used by the gateway and the environment ---------------------------


@dataclass(frozen=True)
class EntityRow:
    version: int
    data: dict[str, Any]


@dataclass(frozen=True)
class StoredReceipt:
    command_hash: str
    semantic_receipt_id: str
    result: dict[str, Any]


class EnvState(Protocol):
    """Transactional per-attempt synthetic state. Every method runs in the gateway's
    transaction, so a mutation, its receipt and its events commit or roll back together."""

    def get(self, entity_type: str, entity_id: str) -> EntityRow | None: ...

    def cas_update(
        self, entity_type: str, entity_id: str, expected_version: int, data: dict[str, Any]
    ) -> bool:
        """Set ``data`` with version+1 only if the row is still at ``expected_version``."""
        ...

    def get_receipt(self, key: str) -> StoredReceipt | None: ...
    def put_receipt(self, key: str, receipt: StoredReceipt) -> None: ...

    def insert_ledger_entry(
        self, semantic_entry_id: str, invoice_id: str, data: dict[str, Any]
    ) -> bool:
        """False when the invoice already has a ledger entry in this attempt (uniqueness)."""
        ...

    def ledger_entry_for(self, invoice_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class StoredCall:
    call_id: str
    call_index: int
    tool_ordinal: int
    input_hash: str
    outcome: dict[str, Any]


@dataclass(frozen=True)
class AttemptCounters:
    calls: int
    steps: int
    logical_time_ms: int
    ordinals: dict[tuple[str, str], int] = field(default_factory=dict)  # (actor, tool) -> count
    applications: dict[str, int] = field(default_factory=dict)  # fault rule id -> times applied


class AttemptTx(Protocol):
    """One fenced transaction for one gateway operation. Entering it locks the run row and
    proves the fencing token (``StaleFence`` otherwise), which also linearizes cancellation."""

    state: EnvState

    def cancel_requested(self) -> bool: ...
    def counters(self) -> AttemptCounters: ...
    def get_call(self, call_id: str) -> StoredCall | None: ...
    def record_call(
        self,
        call_id: str,
        call_index: int,
        tool_ordinal: int,
        actor_id: str,
        tool: str,
        input_hash: str,
        outcome: dict[str, Any],
    ) -> None: ...
    def append(self, event: PendingEvent) -> StoredEvent: ...


class AttemptGate(Protocol):
    """Factory for ``AttemptTx`` bound to one attempt and fencing token."""

    def transaction(self) -> AbstractContextManager[AttemptTx]: ...
