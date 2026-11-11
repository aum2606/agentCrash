"""``invoice-naive``: a deterministic simulation of weak behavior, clearly labelled as such.

One read each of invoice, document and purchase order, no retry, original schema assumed, one
posting attempt (CONTRACTS.md section 6). It is not a measure of any real model. Its
intentionally vulnerable document parser arrives with the injection fixture in M3; on the
healthy path the document is only read.
"""

from agentcrash.domain.runtime import (
    AgentResult,
    AgentRng,
    AgentTask,
    Clock,
    ToolClient,
    ToolRequest,
)

ACTOR = "agent"


class NaiveAgent:
    async def run(
        self, task: AgentTask, tools: ToolClient, clock: Clock, rng: AgentRng
    ) -> AgentResult:
        n = 0

        async def call(tool: str, args: dict[str, object], key: str | None = None):  # type: ignore[no-untyped-def]
            nonlocal n
            n += 1
            return await tools.call(
                ToolRequest(
                    call_id=f"call-{n}", actor_id=ACTOR, tool=tool, args=args, idempotency_key=key
                )
            )

        await tools.step("read invoice")
        invoice = await call("invoice.get", {"invoice_id": task.invoice_id})
        if invoice.status != 200:
            return AgentResult(status="gave_up", summary="invoice unavailable")
        await tools.step("read document")
        await call("document.get", {"invoice_id": task.invoice_id})
        await tools.step("read purchase order")
        po = await call("purchase_order.get", {"po_id": invoice.body["po_id"]})
        if po.status != 200:
            return AgentResult(status="gave_up", summary="purchase order unavailable")
        await tools.step("post invoice")
        posted = await call(
            "ledger.post_invoice",
            {
                "invoice_id": invoice.body["id"],
                "po_id": po.body["id"],
                # Trusts the purchase order's amount and field names: the weakness the schema-change
                # and stale-PO cases expose (a renamed field raises KeyError, an old PO posts 9000).
                "amount_minor": po.body["approved_amount_minor"],
                "currency": po.body["currency"],
                "expected_invoice_version": invoice.body["version"],
                "source_po_version": po.body["version"],
            },
            key=f"post:{task.invoice_id}",
        )
        if posted.status != 200:
            return AgentResult(status="gave_up", summary=f"posting failed with {posted.status}")
        return AgentResult(status="completed", summary="invoice posted")
