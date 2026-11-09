"""Error envelope, request IDs and exception mapping (CONTRACTS.md section 3).

Every non-2xx response uses ``{"error": {code, message, details, request_id, retryable}}``.
Messages are fixed strings: no stack traces, SQL, secrets or echoed request content.
"""

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentcrash.domain.diagnostics import MAX_DIAGNOSTICS, ValidationFailed
from agentcrash.domain.pagination import InvalidCursor
from agentcrash.domain.ports import (
    IdempotencyConflict,
    IdempotencyExpired,
    QueueFull,
    ScenarioConflict,
)

log = logging.getLogger("agentcrash.api")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: list[dict[str, str]] | None = None,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = (details or [])[:MAX_DIAGNOSTICS]
        self.retryable = retryable
        self.headers = dict(headers or {})
        super().__init__(code)


def request_id_of(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def respond(request: Request, err: ApiError) -> JSONResponse:
    body = {
        "error": {
            "code": err.code,
            "message": err.message,
            "details": err.details,
            "request_id": request_id_of(request),
            "retryable": err.retryable,
        }
    }
    headers = {"X-Request-ID": request_id_of(request), **err.headers}
    return JSONResponse(body, status_code=err.status, headers=headers)


class RequestIdMiddleware:
    """Pure ASGI middleware: a fresh server-generated ID per request (client values are
    ignored so they cannot inject into logs) returned as ``X-Request-ID``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    out = []
    for err in exc.errors()[:MAX_DIAGNOSTICS]:
        loc = [str(p) for p in err["loc"][1:]]  # first element is body/query/path
        code = "MISSING_FIELD" if err["type"] == "missing" else "INVALID_VALUE"
        out.append({"path": "/" + "/".join(loc) if loc else "", "code": code})
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return respond(request, exc)

    @app.exception_handler(ValidationFailed)
    async def _domain_validation(request: Request, exc: ValidationFailed) -> JSONResponse:
        details = [d.as_dict() for d in exc.details]
        # Diagnostic messages are fixed strings; they are dropped from the wire to stay minimal.
        details = [{"path": d["path"], "code": d["code"]} for d in details]
        return respond(
            request, ApiError(422, exc.code, "The submitted document is invalid.", details=details)
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        err = ApiError(
            422,
            "REQUEST_VALIDATION_FAILED",
            "The request is invalid.",
            details=_validation_details(exc),
        )
        return respond(request, err)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
        err = ApiError(
            exc.status_code, code, "Request could not be completed.", headers=exc.headers
        )
        return respond(request, err)

    @app.exception_handler(ScenarioConflict)
    async def _scenario_conflict(request: Request, exc: ScenarioConflict) -> JSONResponse:
        msg = "This scenario id and version already exist with different content."
        return respond(request, ApiError(409, "SCENARIO_CONFLICT", msg))

    @app.exception_handler(IdempotencyConflict)
    async def _idem_conflict(request: Request, exc: IdempotencyConflict) -> JSONResponse:
        msg = "This Idempotency-Key was already used with a different request body."
        return respond(request, ApiError(409, "IDEMPOTENCY_CONFLICT", msg))

    @app.exception_handler(IdempotencyExpired)
    async def _idem_expired(request: Request, exc: IdempotencyExpired) -> JSONResponse:
        msg = "This Idempotency-Key belongs to a retained tombstone and cannot be reused."
        return respond(request, ApiError(410, "IDEMPOTENCY_KEY_EXPIRED", msg))

    @app.exception_handler(QueueFull)
    async def _queue_full(request: Request, exc: QueueFull) -> JSONResponse:
        err = ApiError(
            429,
            "QUEUE_FULL",
            "The run queue is full; try again later.",
            retryable=True,
            headers={"Retry-After": "30"},
        )
        return respond(request, err)

    @app.exception_handler(InvalidCursor)
    async def _cursor(request: Request, exc: InvalidCursor) -> JSONResponse:
        return respond(request, ApiError(400, "INVALID_CURSOR", "The cursor is not valid."))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the server log only; the client gets a generic envelope.
        log.error("unhandled error request_id=%s", request_id_of(request), exc_info=exc)
        return respond(request, ApiError(500, "INTERNAL_ERROR", "An internal error occurred."))


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries pointing at the shared envelope model."""
    from agentcrash.domain.ports import ErrorEnvelope

    return {s: {"model": ErrorEnvelope, "description": "Error"} for s in statuses}
