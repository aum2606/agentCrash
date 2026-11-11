"""Reviewed invoice fixtures: state rows plus the benign document (CONTRACTS.md section 6).

The fixture hash covers the state *and* the document text, so editing either changes every
manifest that references it (decision D-026).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcrash.domain.canonical import canonical_hash

MAX_FILE = 64 * 1024
DOCUMENT_ID = "DOC-INV-1001"
DOCUMENT_FILE = "documents/benign.txt"
FAULTS_FILE = "faults.json"
SEED_ENTITIES = ("invoice", "purchase_order", "vendor")


@dataclass(frozen=True)
class FixtureBundle:
    state: dict[str, Any]
    document_text: str
    faults: dict[str, Any] = field(default_factory=dict)  # repository-owned fault fixtures

    @property
    def hash(self) -> str:
        content: dict[str, Any] = {
            "state": self.state,
            "documents": {DOCUMENT_FILE: self.document_text},
        }
        if self.faults:  # absent file keeps the earlier hash
            content["faults"] = self.faults
        return canonical_hash(content)

    def rows(self) -> list[tuple[str, str, int, dict[str, Any]]]:
        """``(entity_type, entity_id, version, data)`` for a fresh attempt. The fixture's own
        ``version`` fields are the row versions, so CAS compares against what the agent reads."""
        out: list[tuple[str, str, int, dict[str, Any]]] = []
        for kind in SEED_ENTITIES:
            data = dict(self.state[kind])
            out.append((kind, data["id"], int(data["version"]), data))
        out.append(
            (
                "document",
                DOCUMENT_ID,
                1,
                {
                    "document_id": DOCUMENT_ID,
                    "invoice_id": self.state["invoice"]["id"],
                    "text": self.document_text,
                },
            )
        )
        return out


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f"{path} exceeds {MAX_FILE} bytes")
    return path.read_text(encoding="utf-8")


def load_bundle(state_file: Path) -> FixtureBundle:
    state = json.loads(_read(state_file))
    if state.get("ledger_entries") or state.get("receipts"):
        raise ValueError("a fixture must start with no ledger entries or receipts")
    faults_file = state_file.parent / FAULTS_FILE
    faults = json.loads(_read(faults_file)) if faults_file.is_file() else {}
    return FixtureBundle(state, _read(state_file.parent / DOCUMENT_FILE), faults)
