"""Static catalog of the synthetic invoice environment's tools and which faults they accept
(CONTRACTS.md sections 5-6). Declarative data only; tool behavior arrives in M2."""

from agentcrash.domain.registry import ToolSpec

ENVIRONMENT_ID = "invoice-env"
ENVIRONMENT_VERSION = 1
TASKS = frozenset({"post-invoice"})
ACTORS = frozenset({"agent"})
FIXTURE_FILES = {"invoice-standard-v1": "fixtures/invoice/v1/standard.json"}

_INVOICE = dict.fromkeys(
    ("id", "po_id", "vendor_id", "amount_minor", "currency", "version", "status")
)
_PO = {
    **dict.fromkeys(
        ("id", "vendor_id", "approved_amount_minor", "currency", "version", "updated_at")
    ),
    "provenance": dict.fromkeys(("snapshot", "version")),
}
_DOCUMENT = dict.fromkeys(("text", "document_id", "source_version"))
_RECEIPT = {
    **dict.fromkeys(("receipt_id", "idempotency_key", "status")),
    "result": dict.fromkeys(("receipt_id", "ledger_entry_id", "invoice_version")),
}

_COMMON = frozenset({"http_error", "delay", "timeout_before_commit"})
_READ = _COMMON | {"schema_mutation", "stale_read"}

TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec("invoice.get", False, False, _READ, _INVOICE),
        ToolSpec("purchase_order.get", False, False, _READ, _PO),
        ToolSpec("document.get", False, False, _READ | {"document_injection"}, _DOCUMENT),
        # Receipt lookups may be faulted only by explicitly targeted rules (plan section 4).
        ToolSpec("receipt.get", False, False, _COMMON | {"schema_mutation"}, _RECEIPT),
        ToolSpec(
            "ledger.post_invoice",
            True,
            False,
            _COMMON | {"timeout_after_commit", "scripted_conflict"},
        ),
        # Always 403 in the MVP; a scenario can never grant it and no fault targets it.
        ToolSpec("vendor.update_bank_account", True, True, frozenset()),
    )
}
