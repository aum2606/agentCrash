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
from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.diagnostics import (
    Diagnostic,
    RunValidationError,
    ScenarioValidationError,
)
from agentcrash.domain.models import (
    CompiledScenario,
    Manifest,
    RunCreate,
    Scenario,
    SourceInfo,
    ToolchainInfo,
)
from agentcrash.domain.pagination import parse_limit
from agentcrash.domain.ports import (
    AttemptNotInRun,
    EvaluationList,
    EventPage,
    RunPage,
    RunRecord,
    ScenarioPage,
    ScenarioSummary,
    Store,
    StoredScenario,
)
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_json_document, load_scenario_document
from agentcrash.scenarios.manifest import build_manifest, manifest_hash, validate_run_create

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
        idempotency_key = request.headers.get("idempotency-key")
        wait = limiter.check()
        if wait is not None:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "Too many creation requests; slow down.",
                retryable=True,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        if idempotency_key is None:
            raise ApiError(
                400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required."
            )
        if not _IDEMPOTENCY_KEY.match(idempotency_key):
            raise ApiError(
                400,
                "IDEMPOTENCY_KEY_INVALID",
                "Idempotency-Key must be 1-128 printable ASCII characters.",
            )
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

    def _manifest_for(run: RunCreate) -> Manifest:
        stored = services.store.get_scenario(run.scenario.id, run.scenario.version)
        if stored is None:
            raise RunValidationError([Diagnostic("/scenario", "UNKNOWN_SCENARIO")])
        # Re-compiling the stored (already normalized) scenario against today's registry
        # catches fixture/grader drift: a run must never start from a stale resolution.
        try:
            compiled: CompiledScenario = compile_scenario(
                stored.scenario.model_dump(mode="json"), services.registry
            )
        except ScenarioValidationError:
            raise RunValidationError(
                [Diagnostic("/scenario", "SCENARIO_NO_LONGER_VALID")]
            ) from None
        if compiled.content_hash != stored.content_hash:
            raise RunValidationError([Diagnostic("/scenario", "SCENARIO_NO_LONGER_VALID")])
        spec = services.registry.agents[(run.agent.id, run.agent.version)]
        services.store.ensure_agent_version(
            spec.id,
            spec.version,
            spec.id,
            canonical_hash(spec.config_model.model_json_schema()),
            spec.source_hash,
        )
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

    app.include_router(v1)

    # --- OpenAPI: expose the raw-body request schemas under components -------------------

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, routes=app.routes, summary=None
            )
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            for model in (Scenario, RunCreate):
                full = _schema_json(model)
                components.update(full.pop("$defs", {}))
                components[model.__name__] = full
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
