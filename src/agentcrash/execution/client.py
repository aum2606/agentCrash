"""Child-side adapters for the gateway transport: ``ToolClient`` and ``Clock``.

Standard library only (no model SDK, no database driver): the child process holds a loopback
URL and an opaque per-attempt capability and nothing else.
"""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from agentcrash.domain.runtime import AttemptEnded, ToolRequest, ToolResponse

# Control refusals after which the agent must stop (the supervisor knows the reason).
_ENDED = frozenset({"STALE_ATTEMPT", "ATTEMPT_CANCELLED", "BUDGET_EXCEEDED", "ATTEMPT_FINISHED"})


class GatewayError(Exception):
    """The gateway refused a request for a reason the agent caused or must report."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GatewayTransport:
    def __init__(self, base_url: str, capability: str, timeout_s: float = 30.0) -> None:
        self._url = base_url.rstrip("/")
        self._capability = capability
        self._timeout = timeout_s

    def _post_sync(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310 - loopback http URL supplied by the supervisor
            self._url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._capability}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                data: dict[str, Any] = json.loads(resp.read())
                return data
        except urllib.error.HTTPError as err:
            try:
                code = json.loads(err.read())["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = f"HTTP_{err.code}"
            if code in _ENDED:
                raise AttemptEnded(code) from None
            raise GatewayError(code) from None

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_sync, path, body)


class HttpToolClient:
    def __init__(self, transport: GatewayTransport) -> None:
        self._t = transport

    async def call(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse.model_validate(
            await self._t.post("/internal/v1/tool-calls", request.model_dump(mode="json"))
        )

    async def step(self, note: str) -> None:
        await self._t.post("/internal/v1/agent/step", {"note": note})


class GatewayClock:
    """Logical time lives in the gateway; this mirrors the last value it returned."""

    def __init__(self, transport: GatewayTransport) -> None:
        self._t = transport
        self._now = 0

    def logical_now_ms(self) -> int:
        return self._now

    async def sleep_ms(self, duration_ms: int) -> None:
        reply = await self._t.post("/internal/v1/clock/advance", {"duration_ms": duration_ms})
        self._now = int(reply["logical_time_ms"])
