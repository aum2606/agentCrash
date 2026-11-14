"""Local API (CONTRACTS.md section 3). Control plane only: it validates, stores and queues work.
Agent code never runs in a request; the worker owns execution (M2).

Depends on ``domain`` ports and ``scenarios`` only; the PostgreSQL store is injected by the
composition root (``agentcrash.bootstrap``).
"""

import contextlib
import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from agentcrash import __version__
from agentcrash.api.errors import (
    ApiError,
    RequestIdMiddleware,
    error_responses,
    install_error_handlers,
)
from agentcrash.api.ratelimit import SlidingWindowLimiter
from agentcrash.api.web import SecurityHeadersMiddleware, install_web
from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.diagnostics import (
    Diagnostic,
    RunValidationError,
    ScenarioValidationError,
)
from agentcrash.domain.models import (
    CompiledScenario,
    ExperimentCreate,
    Manifest,
    RunCreate,
    Scenario,
    ScenarioRef,
    SourceInfo,
    ToolchainInfo,
)
from agentcrash.domain.pagination import parse_limit
from agentcrash.domain.ports import (
    AttemptNotClosed,
    AttemptNotInRun,
    EvaluationList,
    EventPage,
    ExperimentRecord,
    ExportRecord,
    PlannedRun,
    ReplayIncompatible,
    ReplayRecord,
    RunPage,
    RunRecord,
    ScenarioPage,
    ScenarioSummary,
    Store,
    StoredScenario,
)
from agentcrash.domain.registry import Registry
from agentcrash.domain.report import ComparisonReport
from agentcrash.experiments.matrix import Cell, cells, expand, incompatibilities
from agentcrash.experiments.report import build_report, from_evidence, is_finished
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_json_document, load_scenario_document
from agentcrash.scenarios.manifest import (
    build_manifest,
    manifest_hash,
    validate_experiment_create,
    validate_run_create,
)

PRINCIPAL = "local"
MAX_BODY_BYTES = 1 << 20  # 1 MiB (CONTRACTS.md section 1)
RUN_STATUSES = ("queued", "running", "evaluating", "completed", "error")
_IDEMPOTENCY_KEY = re.compile(r"^[\x20-\x7e]{1,128}$")
_YAML_TYPES = {"application/yaml", "application/x-yaml", "text/yaml"}


@dataclass(frozen=True)
class Services:
    store: Store
    registry: Registry
    token: str
    expected_revision: str  # Alembic head the code requires
    provenance: Callable[[], tuple[SourceInfo, ToolchainInfo]]
    queue_cap: int = 100
    create_rate_per_minute: int = 10
    clock: Callable[[], float] = time.monotonic
    # Strict-replay preconditions, injected by the composition root. The default refuses
    # everything, so a Services built without it can never accept a replay.
    replay_check: Callable[[Manifest], list[str]] = lambda manifest: ["REPLAY_CHECK_NOT_CONFIGURED"]
    # Built web client (``web/dist``); None serves the API only.
    web_dir: Path | None = None


class AgentInfo(BaseModel):
    id: str
    version: str
    config_schema: dict[str, Any]
    source_hash: str | None


class AgentList(BaseModel):
    items: list[AgentInfo]


class Health(BaseModel):
    status: str


async def read_body(request: Request) -> bytes:
    """Read the body, enforcing the 1 MiB cap while streaming (not after buffering it all)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "The request body is too large.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "The request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _list_params(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(
            400,
            "UNSUPPORTED_FILTER",
            "Unsupported query parameter.",
            details=[{"path": f"/{name}", "code": "UNSUPPORTED_FILTER"} for name in unknown[:20]],
        )


def _query_docs(*names: str) -> list[dict[str, Any]]:
    """OpenAPI entries for optional query parameters read straight from the request."""
    return [
        {"name": n, "in": "query", "required": False, "schema": {"type": "string"}} for n in names
    ]


def _limit(value: str | None) -> int:
    try:
        return parse_limit(value)
    except ValueError:
        raise ApiError(
            400, "INVALID_PARAMETER", "limit must be an integer from 1 to 200."
        ) from None


def _schema_json(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(ref_template="#/components/schemas/{model}")


def create_app(services: Services) -> FastAPI:
    app = FastAPI(
        title="AgentCrash Lab API",
        version=__version__,
        # The interactive docs would load scripts from a CDN; the API is local-only (ADR-009).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    install_error_handlers(app)
    limiter = SlidingWindowLimiter(services.create_rate_per_minute, 60.0, services.clock)
    bearer = HTTPBearer(auto_error=False, description="Local token from .local/token")

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        supplied = credentials.credentials.encode() if credentials else b""
        ok = credentials is not None and credentials.scheme.lower() == "bearer"
        if not (hmac.compare_digest(supplied, services.token.encode()) and ok):
            raise ApiError(
                401,
                "UNAUTHENTICATED",
                "A valid bearer token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return PRINCIPAL

    # --- health (unauthenticated, no sensitive output) -------------------------------------

    @app.get("/health/live", response_model=Health)
    def live() -> Health:
        return Health(status="ok")

    @app.get("/health/ready", response_model=Health, responses=error_responses(503))
    def ready() -> Health:
        try:
            services.store.ping()
            revision = services.store.schema_revision()
        except Exception:
            raise ApiError(
                503, "NOT_READY", "The database is unavailable.", retryable=True
            ) from None
        if revision != services.expected_revision:
            raise ApiError(
                503,
                "NOT_READY",
                "The database schema is not at the expected revision.",
                retryable=True,
            )
        return Health(status="ok")

    # --- /api/v1 ----------------------------------------------------------------------------

    v1 = APIRouter(
        prefix="/api/v1", dependencies=[Depends(authenticate)], responses=error_responses(401)
    )

    @v1.get("/agents", response_model=AgentList)
    def list_agents() -> AgentList:
        items = [
            AgentInfo(
                id=spec.id,
                version=spec.version,
                config_schema=spec.config_model.model_json_schema(),
                source_hash=spec.source_hash,
            )
            for _, spec in sorted(services.registry.agents.items(), key=lambda kv: kv[0])
        ]
        return AgentList(items=items)

    @v1.post(
        "/scenarios",
        status_code=201,
        response_model=ScenarioSummary,
        responses=error_responses(409, 413, 415, 422)
        | {200: {"model": ScenarioSummary, "description": "Identical version already exists"}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    media: {"schema": {"$ref": "#/components/schemas/Scenario"}}
                    for media in ("application/json", "application/yaml")
                },
            }
        },
    )
    def create_scenario(
        request: Request, response: Response, body: Annotated[bytes, Depends(read_body)]
    ) -> ScenarioSummary:
        media = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media == "application/json":
            document = load_json_document(body)
        elif media in _YAML_TYPES:
            document = load_scenario_document(body)
        else:
            raise ApiError(
                415, "UNSUPPORTED_MEDIA_TYPE", "Use application/json or application/yaml."
            )
        compiled = compile_scenario(document, services.registry)
        result = services.store.put_scenario(compiled)
        summary = result.scenario
        response.status_code = 201 if result.created else 200
        response.headers["Location"] = f"/api/v1/scenarios/{summary.id}/versions/{summary.version}"
        return summary

    @v1.get(
        "/scenarios",
        response_model=ScenarioPage,
        responses=error_responses(400),
        openapi_extra={"parameters": _query_docs("cursor", "limit")},
    )
    def list_scenarios(request: Request) -> ScenarioPage:
        _list_params(request, {"cursor", "limit"})
        q = request.query_params
        return services.store.list_scenarios(q.get("cursor"), _limit(q.get("limit")))

    @v1.get(
        "/scenarios/{scenario_id}/versions/{version}",
        response_model=StoredScenario,
        responses=error_responses(404, 422),
    )
    def get_scenario_version(scenario_id: str, version: str) -> StoredScenario:
        found = (
            services.store.get_scenario(scenario_id, int(version))
            if version.isascii() and version.isdigit() and int(version) < 2**31
            else None
        )
        if found is None:
            raise ApiError(404, "SCENARIO_NOT_FOUND", "No such scenario version.")
        return found

    def _creation_key(request: Request) -> str:
        """Rate-limit a creating request and return its validated Idempotency-Key."""
        wait = limiter.check()
        if wait is not None:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "Too many creation requests; slow down.",
                retryable=True,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        key = request.headers.get("idempotency-key")
        if key is None:
            raise ApiError(
                400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required."
            )
        if not _IDEMPOTENCY_KEY.match(key):
            raise ApiError(
                400,
                "IDEMPOTENCY_KEY_INVALID",
                "Idempotency-Key must be 1-128 printable ASCII characters.",
            )
        return key

    @v1.post(
        "/runs",
        status_code=202,
        response_model=RunRecord,
        responses=error_responses(400, 409, 410, 413, 422, 429),
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "description": "1-128 printable ASCII characters.",
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/RunCreate"}}
                },
            },
        },
    )
    def create_run(
        request: Request, response: Response, body: Annotated[bytes, Depends(read_body)]
    ) -> RunRecord:
        idempotency_key = _creation_key(request)
        run = validate_run_create(load_json_document(body, RunValidationError), services.registry)
        manifest = _manifest_for(run)
        result = services.store.create_run(
            principal=PRINCIPAL,
            route="POST /api/v1/runs",
            key=idempotency_key,
            request_hash=canonical_hash(run.model_dump(mode="json")),
            run=run,
            manifest=manifest,
            manifest_hash=manifest_hash(manifest),
            queue_cap=services.queue_cap,
        )
        response.headers["Location"] = f"/api/v1/runs/{result.run.id}"
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result.run

    def _compiled_for(ref: ScenarioRef, path: str) -> CompiledScenario:
        stored = services.store.get_scenario(ref.id, ref.version)
        if stored is None:
            raise RunValidationError([Diagnostic(path, "UNKNOWN_SCENARIO")])
        # Re-compiling the stored (already normalized) scenario against today's registry
        # catches fixture/grader drift: a run must never start from a stale resolution.
        try:
            compiled: CompiledScenario = compile_scenario(
                stored.scenario.model_dump(mode="json"), services.registry
            )
        except ScenarioValidationError:
            raise RunValidationError([Diagnostic(path, "SCENARIO_NO_LONGER_VALID")]) from None
        if compiled.content_hash != stored.content_hash:
            raise RunValidationError([Diagnostic(path, "SCENARIO_NO_LONGER_VALID")])
        return compiled

    def _ensure_agent(agent_id: str, version: str) -> None:
        spec = services.registry.agents[(agent_id, version)]
        services.store.ensure_agent_version(
            spec.id,
            spec.version,
            spec.id,
            canonical_hash(spec.config_model.model_json_schema()),
            spec.source_hash,
        )

    def _manifest_for(run: RunCreate) -> Manifest:
        compiled = _compiled_for(run.scenario, "/scenario")
        _ensure_agent(run.agent.id, run.agent.version)
        source, toolchain = services.provenance()
        return build_manifest(compiled, run, services.registry, source=source, toolchain=toolchain)

    @v1.get(
        "/runs",
        response_model=RunPage,
        responses=error_responses(400),
        openapi_extra={"parameters": _query_docs("status", "scenario_id", "cursor", "limit")},
    )
    def list_runs(request: Request) -> RunPage:
        _list_params(request, {"status", "scenario_id", "cursor", "limit"})
        q = request.query_params
        status = q.get("status")
        if status is not None and status not in RUN_STATUSES:
            raise ApiError(400, "INVALID_PARAMETER", "Unknown status filter value.")
        return services.store.list_runs(
            status, q.get("scenario_id"), q.get("cursor"), _limit(q.get("limit"))
        )

    @v1.get("/runs/{run_id}", response_model=RunRecord, responses=error_responses(404, 422))
    def get_run(run_id: str) -> RunRecord:
        found = None
        with contextlib.suppress(ValueError):  # not a UUID: same 404 as an unknown run
            found = services.store.get_run(UUID(run_id))
        if found is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        return found

    def _run_id(raw: str) -> UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.") from None

    def _uuid_param(raw: str | None, name: str) -> UUID | None:
        if raw is None:
            return None
        try:
            return UUID(raw)
        except ValueError:
            raise RunValidationError([Diagnostic(f"/{name}", "INVALID_UUID")]) from None

    @v1.post(
        "/runs/{run_id}/cancel",
        response_model=RunRecord,
        status_code=202,
        responses={200: {"model": RunRecord}, **error_responses(404, 413, 422)},
    )
    async def cancel_run(run_id: str, request: Request, response: Response) -> RunRecord:
        body = await read_body(request)
        reason: str | None = None
        if body.strip():
            doc = load_json_document(body, RunValidationError)
            extra = sorted(set(doc) - {"reason"})
            if (
                extra
                or not isinstance(doc.get("reason", ""), str)
                or len(doc.get("reason", "")) > 500
            ):
                raise RunValidationError(
                    [Diagnostic("/reason" if not extra else "/", "INVALID_CANCEL")]
                )
            reason = doc.get("reason")
        result = services.store.cancel_run(_run_id(run_id), reason)
        if result is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        if not result.accepted:
            response.status_code = 200  # already terminal: idempotent no-op
        return result.run

    @v1.get(
        "/runs/{run_id}/events",
        response_model=EventPage,
        responses=error_responses(400, 404, 422),
        openapi_extra={"parameters": _query_docs("attempt_id", "after_seq", "limit")},
    )
    def list_events(run_id: str, request: Request) -> EventPage:
        _list_params(request, {"attempt_id", "after_seq", "limit"})
        q = request.query_params
        try:
            after = int(q.get("after_seq", "0"))
        except ValueError:
            after = -1
        if after < 0:
            raise ApiError(400, "INVALID_PARAMETER", "after_seq must be a non-negative integer.")
        attempt = _uuid_param(q.get("attempt_id"), "attempt_id")
        try:
            page = services.store.list_events(
                _run_id(run_id), attempt, after, _limit(q.get("limit"))
            )
        except AttemptNotInRun:
            raise RunValidationError([Diagnostic("/attempt_id", "ATTEMPT_NOT_IN_RUN")]) from None
        if page is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        return page

    @v1.get(
        "/runs/{run_id}/evaluations",
        response_model=EvaluationList,
        responses=error_responses(404, 422),
        openapi_extra={"parameters": _query_docs("attempt_id")},
    )
    def list_evaluations(run_id: str, request: Request) -> EvaluationList:
        _list_params(request, {"attempt_id"})
        attempt = _uuid_param(request.query_params.get("attempt_id"), "attempt_id")
        items = services.store.list_evaluations(_run_id(run_id), attempt)
        if items is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        return EvaluationList(items=items)

    # --- exports and strict replays ------------------------------------------------------

    def _job_body(
        body: bytes, allowed: dict[str, type], fixed: dict[str, str] | None = None
    ) -> tuple[UUID, dict[str, Any]]:
        """Validate a small ``{attempt_id, ...}`` request body; returns the attempt and fields."""
        doc = load_json_document(body, RunValidationError)
        problems = [Diagnostic(f"/{k}", "UNKNOWN_FIELD") for k in sorted(set(doc) - set(allowed))]
        if "attempt_id" not in doc:
            problems.append(Diagnostic("/attempt_id", "MISSING_FIELD"))
        for name, kind in allowed.items():
            if name in doc and not isinstance(doc[name], kind):
                problems.append(Diagnostic(f"/{name}", "INVALID_VALUE"))
        for name, value in (fixed or {}).items():
            if name not in doc:
                problems.append(Diagnostic(f"/{name}", "MISSING_FIELD"))
            elif doc[name] != value:
                problems.append(Diagnostic(f"/{name}", "INVALID_VALUE"))
        attempt = _uuid_param(
            doc.get("attempt_id") if isinstance(doc.get("attempt_id"), str) else None, "attempt_id"
        )
        if problems or attempt is None:
            raise RunValidationError(problems or [Diagnostic("/attempt_id", "INVALID_VALUE")])
        return attempt, doc

    def _creation_headers(response: Response, location: str, replayed: bool) -> None:
        response.headers["Location"] = location
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"

    _KEY_DOCS = [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "description": "1-128 printable ASCII characters.",
            "schema": {"type": "string", "minLength": 1, "maxLength": 128},
        }
    ]

    @v1.post(
        "/runs/{run_id}/exports",
        status_code=202,
        response_model=ExportRecord,
        responses=error_responses(400, 404, 409, 410, 413, 422, 429),
        openapi_extra={"parameters": _KEY_DOCS},
    )
    def create_export(
        run_id: str,
        request: Request,
        response: Response,
        body: Annotated[bytes, Depends(read_body)],
    ) -> ExportRecord:
        """Body: ``{"attempt_id": uuid, "pin": bool (default true)}``. 409 while the attempt is
        still active. When completed, ``artifact_hash`` is the bundle's SHA-256."""
        key = _creation_key(request)
        attempt, doc = _job_body(body, {"attempt_id": str, "pin": bool})
        pin = bool(doc.get("pin", True))
        run = _run_id(run_id)
        route = "POST /api/v1/runs/{id}/exports"
        try:
            result = services.store.create_export(
                principal=PRINCIPAL,
                route=route,
                key=key,
                request_hash=canonical_hash(
                    {"run_id": str(run), "attempt_id": str(attempt), "pin": pin}
                ),
                run_id=run,
                attempt_id=attempt,
                pin=pin,
            )
        except AttemptNotInRun:
            raise RunValidationError([Diagnostic("/attempt_id", "ATTEMPT_NOT_IN_RUN")]) from None
        except AttemptNotClosed:
            raise ApiError(
                409, "ATTEMPT_NOT_CLOSED", "The attempt is still active; export it once it closes."
            ) from None
        if result is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        _creation_headers(response, f"/api/v1/exports/{result.export.id}", result.replayed)
        return result.export

    @v1.get(
        "/exports/{export_id}",
        response_model=ExportRecord,
        responses=error_responses(404, 422),
    )
    def get_export(export_id: str) -> ExportRecord:
        found = services.store.get_export(_subject_id(export_id, "EXPORT_NOT_FOUND", "export"))
        if found is None:
            raise ApiError(404, "EXPORT_NOT_FOUND", "No such export.")
        return found

    @v1.post(
        "/runs/{run_id}/replays",
        status_code=202,
        response_model=ReplayRecord,
        responses=error_responses(400, 404, 409, 410, 413, 422, 429),
        openapi_extra={"parameters": _KEY_DOCS},
    )
    def create_replay(
        run_id: str,
        request: Request,
        response: Response,
        body: Annotated[bytes, Depends(read_body)],
    ) -> ReplayRecord:
        """Body: ``{"mode": "strict", "attempt_id": uuid}``. 409 if the attempt is still active
        or strict replay is not possible (``REPLAY_INCOMPATIBLE`` lists the reasons)."""
        key = _creation_key(request)
        attempt, _ = _job_body(body, {"attempt_id": str, "mode": str}, {"mode": "strict"})
        run = _run_id(run_id)
        try:
            result = services.store.create_replay(
                principal=PRINCIPAL,
                route="POST /api/v1/runs/{id}/replays",
                key=key,
                request_hash=canonical_hash(
                    {"run_id": str(run), "attempt_id": str(attempt), "mode": "strict"}
                ),
                run_id=run,
                attempt_id=attempt,
                check=services.replay_check,
            )
        except AttemptNotInRun:
            raise RunValidationError([Diagnostic("/attempt_id", "ATTEMPT_NOT_IN_RUN")]) from None
        except AttemptNotClosed:
            raise ApiError(
                409, "ATTEMPT_NOT_CLOSED", "The attempt is still active; replay it once it closes."
            ) from None
        except ReplayIncompatible as err:
            raise ApiError(
                409,
                "REPLAY_INCOMPATIBLE",
                "Strict replay is not possible for this run.",
                details=[{"path": "/", "code": reason} for reason in err.reasons],
            ) from None
        if result is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        _creation_headers(response, f"/api/v1/replays/{result.replay.id}", result.replayed)
        return result.replay

    @v1.get(
        "/replays/{replay_id}",
        response_model=ReplayRecord,
        responses=error_responses(404, 422),
    )
    def get_replay(replay_id: str) -> ReplayRecord:
        found = services.store.get_replay(_subject_id(replay_id, "REPLAY_NOT_FOUND", "replay"))
        if found is None:
            raise ApiError(404, "REPLAY_NOT_FOUND", "No such replay.")
        return found

    def _subject_id(raw: str, code: str, noun: str) -> UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise ApiError(404, code, f"No such {noun}.") from None

    @v1.post(
        "/runs/{run_id}/reruns",
        status_code=202,
        response_model=RunRecord,
        responses=error_responses(400, 404, 409, 410, 413, 422, 429),
        openapi_extra={"parameters": _KEY_DOCS},
    )
    def create_rerun(
        run_id: str,
        request: Request,
        response: Response,
        body: Annotated[bytes, Depends(read_body)],
    ) -> RunRecord:
        """A fresh, independent run from the parent's scenario, agent, budgets and labels,
        optionally with a new ``seed``; the response links back through ``parent_run_id``. This is
        a new execution, not a replay: the agent runs again against the tools."""
        key = _creation_key(request)
        parent = _run_id(run_id)
        seed: Any = None
        override = False
        if body.strip():
            doc = load_json_document(body, RunValidationError)
            extra = [Diagnostic(f"/{k}", "UNKNOWN_FIELD") for k in sorted(set(doc) - {"seed"})]
            if extra:
                raise RunValidationError(extra)
            override = "seed" in doc
            seed = doc.get("seed")
        base = services.store.get_run_create(parent)
        if base is None:
            raise ApiError(404, "RUN_NOT_FOUND", "No such run.")
        document = base.model_dump(mode="json")
        if override:
            document["seed"] = seed
        run = validate_run_create(document, services.registry)
        manifest = _manifest_for(run)
        result = services.store.create_run(
            principal=PRINCIPAL,
            route="POST /api/v1/runs/{id}/reruns",
            key=key,
            request_hash=canonical_hash({"parent_run_id": str(parent), "seed": seed}),
            run=run,
            manifest=manifest,
            manifest_hash=manifest_hash(manifest),
            queue_cap=services.queue_cap,
            parent_run_id=parent,
        )
        response.headers["Location"] = f"/api/v1/runs/{result.run.id}"
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result.run

    @v1.post(
        "/experiments",
        status_code=202,
        response_model=ExperimentRecord,
        responses=error_responses(400, 409, 410, 413, 422, 429),
        openapi_extra={
            "parameters": _KEY_DOCS,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ExperimentCreate"}
                    }
                },
            },
        },
    )
    def create_experiment(
        request: Request, response: Response, body: Annotated[bytes, Depends(read_body)]
    ) -> ExperimentRecord:
        """Atomically creates the experiment, one run per agent for every scenario x seed, and
        their execute jobs. All scenarios and agents are resolved first; any problem rejects the
        whole request. 429 when the matrix would not fit under the queue cap."""
        key = _creation_key(request)
        experiment = validate_experiment_create(
            load_json_document(body, RunValidationError), services.registry
        )
        compiled = {}
        for i, ref in enumerate(experiment.scenario_refs):
            compiled[(ref.id, ref.version)] = _compiled_for(ref, f"/scenario_refs/{i}")
        _ensure_agent(experiment.baseline_agent.id, experiment.baseline_agent.version)
        _ensure_agent(experiment.candidate_agent.id, experiment.candidate_agent.version)
        source, toolchain = services.provenance()  # one snapshot for the whole matrix
        planned: list[PlannedRun] = []
        built: dict[tuple[Cell, str], Manifest] = {}
        for slot in expand(experiment):
            agent = (
                experiment.baseline_agent if slot.role == "baseline" else experiment.candidate_agent
            )
            run = RunCreate(
                schema_version="1.0",
                scenario=ScenarioRef(id=slot.cell.scenario_id, version=slot.cell.scenario_version),
                agent=agent,
                seed=slot.cell.seed,
                mode="mock",
            )
            c = compiled[(slot.cell.scenario_id, slot.cell.scenario_version)]
            manifest = build_manifest(c, run, services.registry, source=source, toolchain=toolchain)
            built[(slot.cell, slot.role)] = manifest
            planned.append(
                PlannedRun(
                    c.content_hash,
                    slot.cell.trial,
                    slot.role,
                    run,
                    manifest,
                    manifest_hash(manifest),
                )
            )
        for cell in cells(experiment):
            problems = incompatibilities(built[(cell, "baseline")], built[(cell, "candidate")])
            if problems:
                raise RunValidationError(
                    [Diagnostic("/candidate_agent", code) for code in problems]
                )
        result = services.store.create_experiment(
            principal=PRINCIPAL,
            route="POST /api/v1/experiments",
            key=key,
            request_hash=canonical_hash(experiment.model_dump(mode="json")),
            config=experiment,
            planned=planned,
            queue_cap=services.queue_cap,
        )
        _creation_headers(response, f"/api/v1/experiments/{result.experiment.id}", result.replayed)
        return result.experiment

    @v1.get(
        "/experiments/{experiment_id}",
        response_model=ExperimentRecord,
        responses=error_responses(404, 422),
    )
    def get_experiment(experiment_id: str) -> ExperimentRecord:
        found = services.store.get_experiment(
            _subject_id(experiment_id, "EXPERIMENT_NOT_FOUND", "experiment")
        )
        if found is None:
            raise ApiError(404, "EXPERIMENT_NOT_FOUND", "No such experiment.")
        return found

    @v1.get(
        "/experiments/{experiment_id}/report",
        response_model=ComparisonReport,
        responses=error_responses(400, 404, 409, 422),
        openapi_extra={"parameters": _query_docs("partial")},
    )
    def get_experiment_report(experiment_id: str, request: Request) -> ComparisonReport:
        _list_params(request, {"partial"})
        partial = request.query_params.get("partial", "false")
        if partial not in ("true", "false"):
            raise ApiError(400, "INVALID_PARAMETER", "partial must be true or false.")
        evidence = services.store.get_experiment_evidence(
            _subject_id(experiment_id, "EXPERIMENT_NOT_FOUND", "experiment")
        )
        if evidence is None:
            raise ApiError(404, "EXPERIMENT_NOT_FOUND", "No such experiment.")
        if partial == "false" and not is_finished(evidence):
            raise ApiError(409, "EXPERIMENT_NOT_FINISHED", "Some runs are still queued or running.")
        return build_report(str(evidence.experiment_id), evidence.config, from_evidence(evidence))

    app.include_router(v1)
    if services.web_dir is not None:
        install_web(app, services.web_dir)

    # --- OpenAPI: expose the raw-body request schemas under components -------------------

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, routes=app.routes, summary=None
            )
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            for model in (Scenario, RunCreate, ExperimentCreate):
                full = _schema_json(model)
                components.update(full.pop("$defs", {}))
                components[model.__name__] = full
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
