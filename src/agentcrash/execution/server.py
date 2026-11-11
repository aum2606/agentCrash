"""Loopback HTTP transport between the child runner and the gateway (CONTRACTS.md section 2).

``POST /internal/v1/tool-calls`` plus three small control routes. It binds 127.0.0.1 only, needs
the per-attempt capability in ``Authorization`` and serves one request at a time: an overlapping
call is refused with ``CONCURRENT_CALL_UNSUPPORTED``. Errors here are infrastructure/control
errors, never synthetic tool failures.
"""

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ValidationError

from agentcrash.domain.ports import StaleFence
from agentcrash.domain.runtime import AgentResult, ToolRequest, ToolTimeout
from agentcrash.execution.gateway import Gateway, GatewayRefusal

MAX_BODY = 256 * 1024
log = logging.getLogger(__name__)


class GatewayServer:
    def __init__(self, gateway: Gateway, capability: str) -> None:
        self.gateway = gateway
        self._capability = capability.encode()
        self._busy = threading.Lock()
        self.fenced = False  # a StaleFence was seen: this worker no longer owns the attempt
        self.infra_error = False  # an unexpected storage error: evidence can no longer be trusted
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib name
                owner._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                owner._send(self, 405, {"error": {"code": "METHOD_NOT_ALLOWED"}})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return int(self._httpd.server_address[1])

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # --- request handling ----------------------------------------------------------------

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(data)
        except OSError:  # the child went away mid-response; nothing to do
            pass

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        header = handler.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(token.encode(), self._capability)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._authorized(handler):
            return self._send(handler, 401, {"error": {"code": "INVALID_CAPABILITY"}})
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(handler, 400, {"error": {"code": "INVALID_REQUEST"}})
        if not 0 <= length <= MAX_BODY:
            return self._send(handler, 413, {"error": {"code": "BODY_TOO_LARGE"}})
        raw = handler.rfile.read(length)
        if not self._busy.acquire(blocking=False):
            return self._send(handler, 409, {"error": {"code": "CONCURRENT_CALL_UNSUPPORTED"}})
        try:
            status, body = self._dispatch(handler.path, raw)
        finally:
            self._busy.release()
        self._send(handler, status, body)

    def _dispatch(self, path: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        try:
            doc = json.loads(raw)
            if path == "/internal/v1/tool-calls":
                response = self.gateway.tool_call(ToolRequest.model_validate(doc))
                return 200, response.model_dump(mode="json")
            if path == "/internal/v1/clock/advance":
                duration = doc.get("duration_ms") if isinstance(doc, dict) else None
                if not isinstance(duration, int) or isinstance(duration, bool):
                    raise GatewayRefusal("INVALID_REQUEST", 422)
                return 200, {"logical_time_ms": self.gateway.advance_clock(duration)}
            if path == "/internal/v1/agent/step":
                note = doc.get("note", "") if isinstance(doc, dict) else None
                if not isinstance(note, str):
                    raise GatewayRefusal("INVALID_REQUEST", 422)
                self.gateway.agent_step(note)
                return 200, {"ok": True}
            if path == "/internal/v1/agent/finish":
                self.gateway.agent_finish(AgentResult.model_validate(doc))
                return 200, {"ok": True}
            return 404, {"error": {"code": "NOT_FOUND"}}
        except ToolTimeout as timeout:  # simulated transport failure, not an HTTP error response
            return 200, {
                "kind": "transport_error",
                "error": {
                    "code": "TOOL_TIMEOUT",
                    "stage": timeout.stage,
                    "elapsed_ms": timeout.elapsed_ms,
                },
            }
        except (ValidationError, ValueError):
            return 422, {"error": {"code": "INVALID_REQUEST"}}
        except GatewayRefusal as refusal:
            return refusal.status, {"error": {"code": refusal.code}}
        except StaleFence:
            self.fenced = True
            return 409, {"error": {"code": "STALE_ATTEMPT"}}
        except Exception:  # authoritative evidence could not be written: stop the attempt
            log.exception("gateway storage failure")
            self.infra_error = True
            return 500, {"error": {"code": "INTERNAL"}}
