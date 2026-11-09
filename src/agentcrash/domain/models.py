"""Strict domain models (CONTRACTS.md sections 4, 5, 7).

Source of truth for the generated JSON Schema (``agentcrash.codegen``). Unknown fields are
rejected, validation is strict (no "1" -> 1 coercion), strings and lists are bounded. Custom
validators raise ``PydanticCustomError`` whose type is the stable diagnostic code.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

SCHEMA_VERSION = "1.0"
FAULT_ENGINE_VERSION = "1"  # engine itself arrives in M3; the version is part of the manifest
TAXONOMY_VERSION = "1"

# --- reusable field types -------------------------------------------------------------------

Id = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", min_length=1, max_length=64)
]
ToolName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", max_length=64)
]
GraderId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.v[0-9]+$", max_length=64)
]
EntityId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,8}-[0-9]{1,10}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")]
DurationMs = Annotated[int, Field(ge=0, le=60_000)]  # all durations are 0-60,000 ms
Ordinal = Annotated[int, Field(ge=1, le=1_000)]


def _probability(v: str) -> str:
    """Exact decimal in [0,1], normalized so equal values hash equally ("1.0" -> "1")."""
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,18})?", v):
        raise PydanticCustomError("INVALID_PROBABILITY", "Probability must be a decimal string")
    d = Decimal(v)
    if d > 1:
        raise PydanticCustomError("INVALID_PROBABILITY", "Probability must be within [0, 1]")
    return format(d.normalize(), "f")


def _seed(v: str) -> str:
    if not re.fullmatch(r"0|[1-9][0-9]{0,19}", v) or int(v) >= 2**64:
        raise PydanticCustomError("INVALID_SEED", "Seed must be an unsigned 64-bit decimal string")
    return v


def _pointer(v: str) -> str:
    """JSON Pointer to an object key: non-root, at most 8 segments, valid escapes only."""
    if not v.startswith("/"):
        raise PydanticCustomError("INVALID_POINTER", "Pointer must be non-root and start with '/'")
    segments = v[1:].split("/")
    if len(segments) > 8:
        raise PydanticCustomError("POINTER_TOO_DEEP", "Pointer depth is limited to 8")
    if any(s == "" or re.search(r"~(?![01])", s) for s in segments):
        raise PydanticCustomError("INVALID_POINTER", "Pointer has an empty segment or bad escape")
    return v


def _bounded_json(v: dict[str, Any]) -> dict[str, Any]:
    """Opaque config/params: JSON only, no floats, shallow and small (hashed data)."""

    def walk(x: Any, depth: int) -> int:
        if depth > 8:
            raise PydanticCustomError("VALUE_TOO_DEEP", "Nesting is limited to 8 levels")
        if isinstance(x, float):
            raise PydanticCustomError("FLOAT_NOT_ALLOWED", "Floats are not allowed; use strings")
        if x is None or isinstance(x, bool | int | str):
            return 1
        if isinstance(x, list):
            return 1 + sum(walk(i, depth + 1) for i in x)
        if isinstance(x, dict) and all(isinstance(k, str) for k in x):
            return 1 + sum(walk(i, depth + 1) for i in x.values())
        raise PydanticCustomError("INVALID_VALUE", "Only JSON values are allowed")

    if walk(v, 0) > 500:
        raise PydanticCustomError("VALUE_TOO_LARGE", "Too many values")
    return v


Probability = Annotated[str, AfterValidator(_probability)]
Seed = Annotated[str, AfterValidator(_seed)]
Pointer = Annotated[str, StringConstraints(max_length=256), AfterValidator(_pointer)]
BoundedJson = Annotated[dict[str, Any], AfterValidator(_bounded_json)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# --- fault effects (discriminated union; stage derives from the type) -----------------------

Stage = Literal["before_dispatch", "after_commit", "after_response"]


class _Effect(Strict):
    stage: ClassVar[Stage]
    terminal: ClassVar[bool] = False  # at most one terminal effect per call and stage
    mutates_payload: ClassVar[bool] = False  # at most one payload mutation per call


class HttpError(_Effect):
    stage: ClassVar[Stage] = "before_dispatch"
    terminal: ClassVar[bool] = True
    type: Literal["http_error"]
    status: Literal[429, 503]
    retry_after_ms: DurationMs | None = None


class TimeoutBeforeCommit(_Effect):
    stage: ClassVar[Stage] = "before_dispatch"
    terminal: ClassVar[bool] = True
    type: Literal["timeout_before_commit"]
    elapsed_ms: DurationMs


class TimeoutAfterCommit(_Effect):
    stage: ClassVar[Stage] = "after_commit"
    terminal: ClassVar[bool] = True
    type: Literal["timeout_after_commit"]
    elapsed_ms: DurationMs


class Delay(_Effect):
    stage: ClassVar[Stage] = "before_dispatch"
    type: Literal["delay"]
    duration_ms: DurationMs


class SchemaMutation(_Effect):
    stage: ClassVar[Stage] = "after_response"
    mutates_payload: ClassVar[bool] = True
    type: Literal["schema_mutation"]
    operation: Literal["remove", "rename"]
    pointer: Pointer
    new_key: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _rename_needs_new_key(self) -> "SchemaMutation":
        if (self.operation == "rename") != (self.new_key is not None):
            raise PydanticCustomError(
                "INVALID_MUTATION", "rename requires new_key; remove must not set it"
            )
        if self.new_key is not None and (
            "/" in self.new_key or self.new_key == self.pointer.rsplit("/", 1)[-1]
        ):
            raise PydanticCustomError("INVALID_MUTATION", "new_key must be a different plain key")
        return self


class StaleRead(_Effect):
    stage: ClassVar[Stage] = "after_response"
    mutates_payload: ClassVar[bool] = True
    type: Literal["stale_read"]
    snapshot_id: Id


class DocumentInjection(_Effect):
    stage: ClassVar[Stage] = "after_response"
    mutates_payload: ClassVar[bool] = True
    type: Literal["document_injection"]
    fixture_id: Id


class ScriptedConflict(_Effect):
    stage: ClassVar[Stage] = "before_dispatch"
    type: Literal["scripted_conflict"]
    target_invoice_id: EntityId
    patch_fixture_id: Id


Effect = Annotated[
    HttpError
    | TimeoutBeforeCommit
    | TimeoutAfterCommit
    | Delay
    | SchemaMutation
    | StaleRead
    | DocumentInjection
    | ScriptedConflict,
    Field(discriminator="type"),
]


class FaultRule(Strict):
    id: Id
    tool: ToolName
    actor_id: Id = "agent"
    on_tool_ordinals: Annotated[list[Ordinal], Field(min_length=1, max_length=50)] | None = None
    probability: Probability = "1"
    max_applications: Annotated[int, Field(ge=1, le=1_000)] = 1
    priority: Annotated[int, Field(ge=0, le=1_000)] = 100
    effect: Effect

    @model_validator(mode="after")
    def _ordinals_unique(self) -> "FaultRule":
        if self.on_tool_ordinals is not None and len(set(self.on_tool_ordinals)) != len(
            self.on_tool_ordinals
        ):
            raise PydanticCustomError("DUPLICATE_ORDINAL", "on_tool_ordinals must be unique")
        return self


# --- scenario -------------------------------------------------------------------------------


class EnvironmentRef(Strict):
    id: Id
    version: Annotated[int, Field(ge=1, le=1_000_000)]
    fixture_id: Id
    fixture_hash: Sha256 | None = None  # resolved by the compiler


class TaskInput(Strict):
    invoice_id: EntityId
    max_source_age_ms: Annotated[int, Field(ge=1, le=10**12)]


def _utc_timestamp(v: str) -> str:
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")  # noqa: DTZ007 - format pins UTC via the Z
    except ValueError:
        raise PydanticCustomError(
            "INVALID_TIMESTAMP", "Expected UTC RFC 3339 like 2026-01-01T00:00:00Z"
        ) from None
    return v


class Task(Strict):
    id: Id
    input: TaskInput
    logical_epoch: Annotated[str, AfterValidator(_utc_timestamp)]


class Policy(Strict):
    allowed_tools: Annotated[list[ToolName], Field(min_length=1, max_length=20)]
    forbidden_tools: Annotated[list[ToolName], Field(max_length=20)] = []


class GraderRef(Strict):
    id: GraderId
    required: bool
    params: BoundedJson = {}


class ExpectedDemo(Strict):
    """Demo-test metadata only; never consulted by graders."""

    verdict: Literal["pass", "fail", "inconclusive"]
    note: Annotated[str, StringConstraints(max_length=500)] = ""


class Scenario(Strict):
    schema_version: Literal["1.0"]
    id: Id
    version: Annotated[int, Field(ge=1, le=1_000_000)]
    description: Annotated[str, StringConstraints(max_length=2_000)] = ""
    environment: EnvironmentRef
    task: Task
    policy: Policy
    faults: Annotated[list[FaultRule], Field(max_length=50)] = []
    graders: Annotated[list[GraderRef], Field(max_length=50)] = []
    expected_demo: Annotated[dict[Id, ExpectedDemo], Field(max_length=10)] | None = None


class CompiledScenario(Strict):
    """Normalized scenario plus content hashes. ``scenario`` is what gets stored and hashed."""

    scenario: Scenario
    content_hash: Sha256
    fixture_hash: Sha256
    grader_set_hash: Sha256


# --- run creation and manifest --------------------------------------------------------------


class ScenarioRef(Strict):
    id: Id
    version: Annotated[int, Field(ge=1, le=1_000_000)]


class AgentRef(Strict):
    id: Id
    version: SemVer
    config: BoundedJson = {}


class Budgets(Strict):
    """Defaults equal the MVP caps; requests may only lower them (raising is local config)."""

    wall_ms: Annotated[int, Field(ge=1, le=120_000)] = 120_000
    logical_ms: Annotated[int, Field(ge=1, le=60_000)] = 60_000
    tool_calls: Annotated[int, Field(ge=1, le=50)] = 50
    steps: Annotated[int, Field(ge=1, le=20)] = 20


def _labels(v: dict[str, str]) -> dict[str, str]:
    if len(v) > 10:
        raise PydanticCustomError("TOO_MANY_LABELS", "At most 10 labels")
    if any(not 1 <= len(k) <= 32 or len(val) > 128 for k, val in v.items()):
        raise PydanticCustomError("LABEL_TOO_LONG", "Label keys <=32 and values <=128 characters")
    return v


class RunCreate(Strict):
    schema_version: Literal["1.0"]
    scenario: ScenarioRef
    agent: AgentRef
    seed: Seed
    mode: Literal["mock"]
    budgets: Budgets = Budgets()
    labels: Annotated[dict[str, str], AfterValidator(_labels)] = {}


class FixtureInfo(Strict):
    id: Id
    hash: Sha256


class ResolvedAgent(Strict):
    id: Id
    version: SemVer
    config: BoundedJson
    config_hash: Sha256
    source_hash: Sha256 | None


class ResolvedGrader(Strict):
    id: GraderId
    version: int
    required: bool
    params: BoundedJson


class SourceInfo(Strict):
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] | None
    dirty: bool


class ToolchainInfo(Strict):
    python: Annotated[str, StringConstraints(max_length=32)]
    lock_hash: Sha256 | None


class RedactionInfo(Strict):
    policy_version: Annotated[str, StringConstraints(max_length=16)] = "1"


class Manifest(Strict):
    """Immutable run manifest; ``manifest_hash`` is the canonical hash of this document."""

    manifest_version: Literal["1.0"]
    scenario: Scenario
    scenario_hash: Sha256
    fixture: FixtureInfo
    agent: ResolvedAgent
    graders: list[ResolvedGrader]
    grader_set_hash: Sha256
    seed: Seed
    mode: Literal["mock"]
    logical_epoch: str
    budgets: Budgets
    canonicalization_version: Literal["acl-c14n-v1"]
    fault_engine_version: str
    taxonomy_version: str
    source: SourceInfo
    toolchain: ToolchainInfo
    redaction: RedactionInfo
    replay_capability: Literal["full", "partial", "none"]
