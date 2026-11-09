"""Ports and read models between the API and persistence (CONTRACTS.md sections 2-4).

The API depends on ``Store`` only; the PostgreSQL implementation lives in ``storage`` and is
wired in by the composition root. Methods are synchronous (decision D-019).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field

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
    def list_runs(
        self, status: str | None, scenario_id: str | None, cursor: str | None, limit: int
    ) -> RunPage: ...
