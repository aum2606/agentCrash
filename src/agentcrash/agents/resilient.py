"""``invoice-resilient``: the deterministic reference agent that handles the suite's faults.

CONTRACTS.md section 6: typed body checks, age-based freshness, at most three attempts per
transient operation with the contract's backoff, same-key write retry with receipt reconciliation,
CAS refresh, documents treated as data. It shows what a careful agent does with this harness; it
is not a measure of any real model. It reads the document (it is part of the task) but never acts
on its text, and it never learns the hidden expected purchase-order version: it judges the source
only by shape, age (against the logical task time) and agreement with the invoice.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from agentcrash.domain.runtime import (
    AgentResult,
    AgentRng,
    AgentTask,
    Clock,
    ToolClient,
    ToolRequest,
    ToolResponse,
    ToolTimeout,
)

ACTOR = "agent"
MAX_ATTEMPTS = 3
TRANSIENT = frozenset({429, 503})
INVOICE_TYPES = {"id": str, "po_id": str, "amount_minor": int, "currency": str, "version": int}
PO_TYPES = {
    "id": str,
    "approved_amount_minor": int,
    "currency": str,
    "version": int,
    "updated_at": str,
}


class _GiveUp(Exception):
    """Stop and report inability. The message is a fixed string, never tool data."""


def _typed(body: dict[str, Any], types: dict[str, type]) -> bool:
    return all(
        isinstance(body.get(k), t) and not (t is int and isinstance(body.get(k), bool))
        for k, t in types.items()
    )


def _error_code(resp: ToolResponse) -> str:
    error = resp.body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


class ResilientAgent:
    async def run(
        self, task: AgentTask, tools: ToolClient, clock: Clock, rng: AgentRng
    ) -> AgentResult:
        session = _Session(task, tools, clock, rng)
        try:
            await session.execute()
        except _GiveUp as stop:
            return AgentResult(status="gave_up", summary=str(stop))
        return AgentResult(status="completed", summary="invoice posted")


class _Session:
    def __init__(self, task: AgentTask, tools: ToolClient, clock: Clock, rng: AgentRng) -> None:
        self.task, self.tools, self.clock, self.rng = task, tools, clock, rng
        self.n = 0
        self.seen_ms = 0  # logical time as last reported by the gateway
        self.epoch = datetime.fromisoformat(task.logical_epoch.replace("Z", "+00:00"))

    # --- plumbing ------------------------------------------------------------------------

    async def _call(self, tool: str, args: dict[str, Any], key: str | None = None) -> ToolResponse:
        self.n += 1
        resp = await self.tools.call(
            ToolRequest(
                call_id=f"call-{self.n}",
                actor_id=ACTOR,
                tool=tool,
                args=args,
                idempotency_key=key,
            )
        )
        self.seen_ms = max(self.seen_ms, resp.meta.logical_time_ms)
        return resp

    async def _backoff(self, attempt: int, resp: ToolResponse | None) -> None:
        """Wait ``max(retry_after, min(250*2^(attempt-1), 2000)) + jitter`` logical ms."""
        error = resp.body.get("error") if resp is not None else None
        hint = error.get("retry_after_ms") if isinstance(error, dict) else None
        retry_after = hint if isinstance(hint, int) and not isinstance(hint, bool) else 0
        delay = max(retry_after, min(250 * 2 ** (attempt - 1), 2000)) + self.rng.jitter_ms(100)
        await self.clock.sleep_ms(delay)
        self.seen_ms = max(self.seen_ms, self.clock.logical_now_ms())

    async def _transient(
        self, tool: str, args: dict[str, Any], what: str, key: str | None = None
    ) -> ToolResponse:
        """A read or other safe call, retried on 429/503/timeout up to three attempts."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp: ToolResponse | None
            try:
                resp = await self._call(tool, args, key)
            except ToolTimeout:
                resp = None
            if resp is not None and resp.status not in TRANSIENT:
                return resp
            if attempt == MAX_ATTEMPTS:
                raise _GiveUp(f"{what} stayed unavailable after {MAX_ATTEMPTS} attempts")
            await self._backoff(attempt, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    # --- the task ------------------------------------------------------------------------

    async def _read_invoice(self) -> dict[str, Any]:
        for _ in range(2):  # one re-read if the body does not have the expected shape
            resp = await self._transient(
                "invoice.get", {"invoice_id": self.task.invoice_id}, "invoice read"
            )
            if resp.status != 200:
                raise _GiveUp(f"invoice read failed with status {resp.status}")
            if _typed(resp.body, INVOICE_TYPES):
                return resp.body
        raise _GiveUp("invoice response did not match the expected schema")

    def _po_problem(self, po: dict[str, Any], invoice: dict[str, Any]) -> str | None:
        if not _typed(po, PO_TYPES):
            return "schema"
        try:
            updated = datetime.fromisoformat(po["updated_at"].replace("Z", "+00:00")).astimezone(
                UTC
            )
        except ValueError:
            return "schema"
        now = self.epoch + timedelta(milliseconds=max(self.seen_ms, self.clock.logical_now_ms()))
        if (now - updated) / timedelta(milliseconds=1) > self.task.max_source_age_ms:
            return "stale"
        if po["id"] != invoice["po_id"]:
            return "wrong_order"
        if (po["approved_amount_minor"], po["currency"]) != (
            invoice["amount_minor"],
            invoice["currency"],
        ):
            return "amount_mismatch"
        return None

    async def _read_po(self, invoice: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"po_id": invoice["po_id"]}
        for forced in (False, True):  # at most one forced refresh
            resp = await self._transient(
                "purchase_order.get",
                {**args, "force_refresh": True} if forced else args,
                "purchase order read",
            )
            if resp.status != 200:
                raise _GiveUp(f"purchase order read failed with status {resp.status}")
            problem = self._po_problem(resp.body, invoice)
            if problem is None:
                return resp.body
            if forced or problem in ("wrong_order", "amount_mismatch"):
                raise _GiveUp(f"purchase order cannot be used ({problem})")
            await self.tools.step(f"refresh purchase order ({problem})")
        raise AssertionError("unreachable")  # pragma: no cover

    async def _reconcile(self, key: str) -> bool:
        """True if the write with ``key`` is already committed (receipt exists)."""
        resp = await self._transient("receipt.get", {"idempotency_key": key}, "receipt lookup")
        if resp.status == 200:
            return True
        if resp.status == 404:
            return False
        raise _GiveUp(f"receipt lookup failed with status {resp.status}")

    async def _post_once(self, args: dict[str, Any], key: str) -> ToolResponse | None:
        """Post with transient retries under one key. None means the write is confirmed
        committed through its receipt although the response was lost."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp: ToolResponse | None
            try:
                resp = await self._call("ledger.post_invoice", args, key)
            except ToolTimeout:
                if await self._reconcile(key):
                    return None
                resp = None
            if resp is not None and resp.status not in TRANSIENT:
                return resp
            if attempt == MAX_ATTEMPTS:
                raise _GiveUp(f"posting stayed unavailable after {MAX_ATTEMPTS} attempts")
            await self._backoff(attempt, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    async def execute(self) -> None:
        task = self.task
        await self.tools.step("read invoice")
        invoice = await self._read_invoice()
        await self.tools.step("read document")
        # The document is data: fetched because the task includes it, never interpreted.
        await self._transient("document.get", {"invoice_id": task.invoice_id}, "document read")
        await self.tools.step("read purchase order")
        po = await self._read_po(invoice)

        key = f"post:{task.invoice_id}"
        for _ in range(MAX_ATTEMPTS):  # CAS refreshes
            await self.tools.step("post invoice")
            args = {
                "invoice_id": invoice["id"],
                "po_id": po["id"],
                "amount_minor": invoice["amount_minor"],
                "currency": invoice["currency"],
                "expected_invoice_version": invoice["version"],
                "source_po_version": po["version"],
            }
            resp = await self._post_once(args, key)
            if resp is None or resp.status == 200:
                return
            if resp.status == 409 and _error_code(resp) == "VERSION_CONFLICT":
                await self.tools.step("refresh invoice after version conflict")
                fresh = await self._read_invoice()
                if (fresh["amount_minor"], fresh["currency"]) != (
                    invoice["amount_minor"],
                    invoice["currency"],
                ):
                    raise _GiveUp("invoice changed materially during posting")
                invoice = fresh
                continue
            raise _GiveUp(f"posting was refused with status {resp.status}")
        raise _GiveUp("posting kept conflicting after several refreshes")
