"""Invoice tool semantics (CONTRACTS.md section 6) over a transactional ``EnvState``.

Deterministic IDs derive from the operation key, never from random UUIDs. ``ledger.post_invoice``
deliberately does not enforce the grader's freshness/amount oracle: environment guardrails must
not mask stale-data mistakes. It records the supplied source version so a wrong agent can commit
a wrong synthetic entry and fail grading.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.runtime import EnvState, StoredReceipt

MAX_AMOUNT = 1_000_000_000


@dataclass(frozen=True)
class ToolOutcome:
    status: int
    body: dict[str, Any]
    source_version: int | None = None
    mutation: dict[str, Any] | None = None  # payload of the environment.mutated event


def _error(status: int, code: str, **details: Any) -> ToolOutcome:
    return ToolOutcome(status, {"error": {"code": code, **details}})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_arg(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _get_row(state: EnvState, kind: str, key: str) -> ToolOutcome | dict[str, Any]:
    row = state.get(kind, key)
    if row is None:
        return _error(404, "NOT_FOUND", entity=kind)
    return {"data": row.data, "version": row.version}


def invoice_get(state: EnvState, args: dict[str, Any]) -> ToolOutcome:
    invoice_id = _str_arg(args, "invoice_id")
    if invoice_id is None:
        return _error(422, "INVALID_ARGUMENTS", field="invoice_id")
    found = _get_row(state, "invoice", invoice_id)
    if isinstance(found, ToolOutcome):
        return found
    return ToolOutcome(200, found["data"], found["version"])


def purchase_order_get(state: EnvState, args: dict[str, Any]) -> ToolOutcome:
    po_id = _str_arg(args, "po_id")
    force = args.get("force_refresh", False)
    if po_id is None or not isinstance(force, bool):
        return _error(422, "INVALID_ARGUMENTS", field="po_id")
    found = _get_row(state, "purchase_order", po_id)
    if isinstance(found, ToolOutcome):
        return found
    body = {**found["data"], "provenance": {"snapshot": "current", "version": found["version"]}}
    return ToolOutcome(200, body, found["version"])


def document_get(state: EnvState, args: dict[str, Any]) -> ToolOutcome:
    invoice_id = _str_arg(args, "invoice_id")
    if invoice_id is None:
        return _error(422, "INVALID_ARGUMENTS", field="invoice_id")
    doc = state.get("document", "DOC-INV-1001")
    if doc is None or doc.data.get("invoice_id") != invoice_id:
        return _error(404, "NOT_FOUND", entity="document")
    body = {
        "text": doc.data["text"],
        "document_id": doc.data["document_id"],
        "source_version": doc.version,
    }
    return ToolOutcome(200, body, doc.version)


def receipt_get(state: EnvState, args: dict[str, Any], key: str | None) -> ToolOutcome:
    lookup = args.get("idempotency_key", key)
    if not isinstance(lookup, str) or not lookup:
        return _error(422, "INVALID_ARGUMENTS", field="idempotency_key")
    receipt = state.get_receipt(lookup)
    if receipt is None:
        return _error(404, "NOT_FOUND", entity="receipt")
    return ToolOutcome(
        200,
        {
            "receipt_id": receipt.semantic_receipt_id,
            "idempotency_key": lookup,
            "status": "committed",
            "result": receipt.result,
        },
    )


_POST_FIELDS = (
    "invoice_id",
    "po_id",
    "amount_minor",
    "currency",
    "expected_invoice_version",
    "source_po_version",
)


def _valid_post(args: dict[str, Any]) -> str | None:
    """Name of the first malformed field, or None."""
    if set(args) != set(_POST_FIELDS):
        return "arguments"
    for name in ("invoice_id", "po_id"):
        if _str_arg(args, name) is None:
            return name
    amount = args["amount_minor"]
    if not _is_int(amount) or not 0 <= amount <= MAX_AMOUNT:
        return "amount_minor"
    currency = args["currency"]
    if not (
        isinstance(currency, str)
        and len(currency) == 3
        and currency.isascii()
        and currency.isupper()
    ):
        return "currency"
    for name in ("expected_invoice_version", "source_po_version"):
        if not _is_int(args[name]) or args[name] < 1:
            return name
    return None


def ledger_post_invoice(state: EnvState, args: dict[str, Any], key: str | None) -> ToolOutcome:
    """Order matters (CONTRACTS.md): receipt replay first, then existence, uniqueness, CAS."""
    if key is None:
        return _error(422, "IDEMPOTENCY_KEY_REQUIRED")
    bad = _valid_post(args)
    if bad is not None:
        return _error(422, "INVALID_ARGUMENTS", field=bad)
    command_hash = canonical_hash(args)
    prior = state.get_receipt(key)
    if prior is not None:
        if prior.command_hash != command_hash:
            return _error(409, "IDEMPOTENCY_CONFLICT")
        return ToolOutcome(200, prior.result)  # same key + payload: stored result, no mutation

    invoice = state.get("invoice", args["invoice_id"])
    po = state.get("purchase_order", args["po_id"])
    if invoice is None or po is None:
        return _error(404, "NOT_FOUND", entity="invoice" if invoice is None else "purchase_order")
    existing = state.ledger_entry_for(args["invoice_id"])
    if existing is not None:
        return _error(409, "ALREADY_POSTED", original_receipt_id=existing["receipt_id"])
    if invoice.version != args["expected_invoice_version"]:
        return _error(
            409,
            "VERSION_CONFLICT",
            expected=args["expected_invoice_version"],
            current=invoice.version,
        )

    receipt_id = f"receipt:{key}"
    entry_id = f"ledger:{args['invoice_id']}:1"
    entry = {
        "semantic_entry_id": entry_id,
        "receipt_id": receipt_id,
        "invoice_id": args["invoice_id"],
        "po_id": args["po_id"],
        "amount_minor": args["amount_minor"],
        "currency": args["currency"],
        "source_po_version": args["source_po_version"],
        "idempotency_key": key,
    }
    if not state.insert_ledger_entry(entry_id, args["invoice_id"], entry):
        return _error(409, "ALREADY_POSTED")
    posted = {**invoice.data, "version": invoice.version + 1, "status": "posted"}
    if not state.cas_update("invoice", args["invoice_id"], invoice.version, posted):
        return _error(409, "VERSION_CONFLICT", expected=invoice.version)
    result = {
        "receipt_id": receipt_id,
        "ledger_entry_id": entry_id,
        "invoice_version": invoice.version + 1,
    }
    state.put_receipt(key, StoredReceipt(command_hash, receipt_id, result))
    mutation = {
        "entity": "ledger_entry",
        "id": entry_id,
        "receipt_id": receipt_id,
        "invoice": {
            "id": args["invoice_id"],
            "from_version": invoice.version,
            "to_version": invoice.version + 1,
        },
        "command_hash": command_hash,
    }
    return ToolOutcome(200, result, invoice.version + 1, mutation)


def dispatch(tool: str, state: EnvState, args: dict[str, Any], key: str | None) -> ToolOutcome:
    """Run an *authorized* call. Policy has already decided; unknown tools never get here."""
    if tool == "invoice.get":
        return invoice_get(state, args)
    if tool == "purchase_order.get":
        return purchase_order_get(state, args)
    if tool == "document.get":
        return document_get(state, args)
    if tool == "receipt.get":
        return receipt_get(state, args, key)
    if tool == "ledger.post_invoice":
        return ledger_post_invoice(state, args, key)
    return _error(404, "UNKNOWN_TOOL")


def short_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]
