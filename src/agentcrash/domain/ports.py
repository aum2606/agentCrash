"""Ports and read models between the API and persistence (CONTRACTS.md sections 2-4).

The API depends on ``Store`` only; the PostgreSQL implementation lives in ``storage`` and is
wired in by the composition root. Methods are synchronous (decision D-019).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import Field

from agentcrash.domain.evidence import Finding, Grade
from agentcrash.domain.models import (
    CompiledScenario,
    ExperimentCreate,
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


JobStatus = Literal["queued", "running", "completed", "error"]


class ExportRecord(Strict):
    """An export job. ``artifact_hash`` (the bundle's SHA-256) is set once it is completed."""

    id: UUID
    run_id: UUID
    attempt_id: UUID
    status: JobStatus
    pin: bool
    artifact_hash: Sha256 | None
    error_code: str | None
    created_at: datetime
    closed_at: datetime | None


class ReplayRecord(Strict):
    """A strict replay job. ``completed`` with ``divergence`` null means the agent matched the
    recording exactly; ``completed`` with a divergence reports the first difference; ``error``
    means the replay could not run (``error_code`` says why)."""

    id: UUID
    run_id: UUID
    attempt_id: UUID
    mode: Literal["strict"] = "strict"
    status: JobStatus
    adapter_hash: Sha256 | None
    consumed_events: int | None
    total_events: int | None
    divergence: dict[str, Any] | None
    error_code: str | None
    created_at: datetime
    closed_at: datetime | None


class CreateExportResult(Strict):
    export: ExportRecord
    replayed: bool


class CreateReplayResult(Strict):
    replay: ReplayRecord
    replayed: bool


class RoleProgress(Strict):
    """Run counts for one agent role, by lifecycle status."""

    total: int
    queued: int
    running: int
    evaluating: int
    completed: int
    error: int


class ExperimentSlot(Strict):
    scenario_id: Id
    scenario_version: int
    seed: str
    trial: int
    role: Literal["baseline", "candidate"]
    run_id: UUID
    status: RunStatus
    verdict: Verdict | None


class ExperimentRecord(Strict):
    """Matrix progress. ``status`` is derived: ``running`` until every run is terminal, then
    ``completed`` (``error`` if any run ended in error). The report (verdicts, gate) is a
    separate resource."""

    id: UUID
    name: str
    gate_policy_id: Literal["mock-release.v1"]
    config_hash: Sha256
    status: Literal["running", "completed", "error"]
    expected_runs: int
    progress: dict[Literal["baseline", "candidate"], RoleProgress]
    slots: list[ExperimentSlot]
    created_at: datetime


class CreateExperimentResult(Strict):
    experiment: ExperimentRecord
    replayed: bool


@dataclass(frozen=True)
class PlannedRun:
    """One slot, fully resolved by the API before anything is written."""

    scenario_hash: str
    trial: int
    role: Literal["baseline", "candidate"]
    run: RunCreate
    manifest: Manifest
    manifest_hash: str


class AttemptNotClosed(Exception):
    """The attempt is still active; its evidence is not final."""


class ReplayIncompatible(Exception):
    """Strict replay is not possible for this run; ``reasons`` are stable codes."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


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
class SubjectClaim:
    """A leased export or replay job and the export/replay row it works for."""

    job_id: UUID
    run_id: UUID
    attempt_id: UUID
    subject_id: UUID


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
        parent_run_id: UUID | None = None,
    ) -> CreateRunResult: ...
    def get_run(self, run_id: UUID) -> RunRecord | None: ...
    def get_run_create(self, run_id: UUID) -> RunCreate | None: ...
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

    def create_export(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run_id: UUID,
        attempt_id: UUID,
        pin: bool,
    ) -> CreateExportResult | None: ...
    def get_export(self, export_id: UUID) -> ExportRecord | None: ...
    def create_replay(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run_id: UUID,
        attempt_id: UUID,
        check: Callable[[Manifest], list[str]],
    ) -> CreateReplayResult | None: ...
    def get_replay(self, replay_id: UUID) -> ReplayRecord | None: ...

    def create_experiment(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        config: ExperimentCreate,
        planned: list[PlannedRun],
        queue_cap: int,
    ) -> CreateExperimentResult: ...
    def get_experiment(self, experiment_id: UUID) -> ExperimentRecord | None: ...
