"""Ports and read models between the API and persistence (CONTRACTS.md sections 2-4).

The API depends on ``Store`` only; the PostgreSQL implementation lives in ``storage`` and is
wired in by the composition root. Methods are synchronous (decision D-019).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import Field

from agentcrash.domain.evidence import Finding, Grade
from agentcrash.domain.models import (
    CompiledScenario,
    Id,
    Manifest,
    RunCreate,
    Scenario,
    ScenarioRef,
    SemVer,
    Sha256,
    Strict,
)

RunStatus = Literal["queued", "running", "evaluating", "completed", "error"]
Verdict = Literal["pass", "fail", "inconclusive"]


class ScenarioSummary(Strict):
    id: Id
    version: int
    content_hash: Sha256
    description: str
    created_at: datetime


class StoredScenario(ScenarioSummary):
    scenario: Scenario


class AttemptSummary(Strict):
    id: UUID
    ordinal: int
    termination: str | None


class AgentIdentity(Strict):
    id: Id
    version: SemVer


class RunRecord(Strict):
    id: UUID
    status: RunStatus
    verdict: Verdict | None
    manifest_hash: Sha256
    selected_attempt_id: UUID | None
    attempts: list[AttemptSummary]
    parent_run_id: UUID | None
    replay_capability: Literal["full", "partial", "none"]
    scenario: ScenarioRef
    agent: AgentIdentity
    seed: str
    labels: dict[str, str]
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None = None  # additive (D-025): why a run ended in ``error``


class ScenarioPage(Strict):
    items: list[ScenarioSummary]
    next_cursor: str | None


class RunPage(Strict):
    items: list[RunRecord]
    next_cursor: str | None


class PutScenarioResult(Strict):
    scenario: ScenarioSummary
    created: bool


class CreateRunResult(Strict):
    run: RunRecord
    replayed: bool


class ErrorBody(Strict):
    code: str
    message: str
    details: Annotated[list[dict[str, str]], Field(max_length=50)]
    request_id: str
    retryable: bool


class ErrorEnvelope(Strict):
    error: ErrorBody


class EventRecord(Strict):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    attempt_id: UUID
    seq: int
    type: str
    wall_time: datetime
    logical_time_ms: int
    actor_id: str | None
    call_id: str | None
    trace_id: str | None = None
    span_id: str | None = None
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str


class EventPage(Strict):
    items: list[EventRecord]
    next_after_seq: int
    has_more: bool
    attempt_closed: bool


class EvaluationRecord(Strict):
    id: UUID
    attempt_id: UUID
    grader_set_hash: str
    snapshot_hash: str | None
    verdict: Verdict
    grades: list[Grade]
    primary_reason: str | None
    findings: list[Finding]
    evaluated_at: datetime


class EvaluationList(Strict):
    items: list[EvaluationRecord]


class CancelResult(Strict):
    run: RunRecord
    accepted: bool  # False: the run was already terminal (HTTP 200 instead of 202)


class AttemptNotInRun(Exception):
    """The requested attempt does not belong to the run."""


class ScenarioConflict(Exception):
    """Same id/version already stored with different content."""


class IdempotencyConflict(Exception):
    """Same key reused with a different request body."""


class IdempotencyExpired(Exception):
    """Key's resource was retained as a tombstone; never re-execute an old key."""


class QueueFull(Exception):
    """Admission cap reached."""


class StaleFence(Exception):
    """The caller's fencing token is no longer the attempt's active one (or the attempt closed)."""


# Termination reasons a worker may report; ``worker_lost`` is written only by the reaper.
WORKER_TERMINATIONS = (
    "agent_completed",
    "agent_error",
    "budget_exceeded",
    "deadline_exceeded",
    "cancelled",
)


@dataclass(frozen=True)
class Claim:
    """A leased execute job and the fresh attempt created for it."""

    job_id: UUID
    run_id: UUID
    attempt_id: UUID
    ordinal: int
    fencing_token: int
    owner: str
    lease_until: datetime


@dataclass(frozen=True)
class GradeClaim:
    job_id: UUID
    run_id: UUID
    attempt_id: UUID


@dataclass(frozen=True)
class Reaped:
    """One expired lease closed by the reaper. ``requeued`` False means attempts were exhausted
    and the run is now ``error`` (INFRA_WORKER_LOST)."""

    run_id: UUID
    attempt_id: UUID
    ordinal: int
    requeued: bool


class Store(Protocol):
    def ping(self) -> None: ...
    def schema_revision(self) -> str | None: ...

    def put_scenario(self, compiled: CompiledScenario) -> PutScenarioResult: ...
    def get_scenario(self, scenario_id: str, version: int) -> StoredScenario | None: ...
    def list_scenarios(self, cursor: str | None, limit: int) -> ScenarioPage: ...

    def ensure_agent_version(
        self,
        agent_id: str,
        version: str,
        adapter_registry_key: str,
        config_schema_hash: str,
        source_hash: str | None,
    ) -> None: ...

    def create_run(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run: RunCreate,
        manifest: Manifest,
        manifest_hash: str,
        queue_cap: int,
    ) -> CreateRunResult: ...
    def get_run(self, run_id: UUID) -> RunRecord | None: ...
    def cancel_run(self, run_id: UUID, reason: str | None) -> CancelResult | None: ...
    def list_events(
        self, run_id: UUID, attempt_id: UUID | None, after_seq: int, limit: int
    ) -> EventPage | None: ...
    def list_evaluations(
        self, run_id: UUID, attempt_id: UUID | None
    ) -> list[EvaluationRecord] | None: ...

    def list_runs(
        self, status: str | None, scenario_id: str | None, cursor: str | None, limit: int
    ) -> RunPage: ...
