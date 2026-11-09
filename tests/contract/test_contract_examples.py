"""Examples in CONTRACTS.md are verified against the code, not copied by hand: a contract edit
that no longer validates (or a code change that drifts from the contract) fails here."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from agentcrash.domain.ports import ErrorEnvelope, RunRecord
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.scenarios.manifest import validate_run_create

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = (ROOT / "CONTRACTS.md").read_text(encoding="utf-8")
BLOCKS = re.findall(r"```(json|yaml)\n(.*?)```", CONTRACTS, flags=re.DOTALL)


def block(kind: str, marker: str) -> str:
    found = [text for k, text in BLOCKS if k == kind and marker in text]
    assert len(found) == 1, f"expected exactly one {kind} block containing {marker!r}"
    return str(found[0])


def test_run_create_example_validates(registry: Registry) -> None:
    example = json.loads(block("json", '"budgets"'))
    run = validate_run_create(example, registry)
    assert run.scenario.id == "invoice-429" and run.seed == "42"


def test_scenario_example_is_the_shipped_scenario_verbatim() -> None:
    contract = load_scenario_document(block("yaml", "id: invoice-429").encode())
    shipped = load_scenario_document((ROOT / "scenarios/invoice/v1/invoice-429.yaml").read_bytes())
    assert contract == shipped


def test_fixture_example_is_the_shipped_fixture() -> None:
    contract = json.loads(block("json", '"ledger_entries"'))
    shipped = json.loads((ROOT / "fixtures/invoice/v1/standard.json").read_text(encoding="utf-8"))
    assert contract == shipped


def test_error_envelope_example_matches_the_model() -> None:
    example = json.loads(block("json", "SCENARIO_VALIDATION_FAILED"))
    ErrorEnvelope.model_validate(example)
    assert set(example["error"]) == {"code", "message", "details", "request_id", "retryable"}


def test_run_response_example_is_a_subset_of_the_run_record() -> None:
    """Placeholders are illustrative (CONTRACTS section 4); the response may add fields."""
    example: dict[str, Any] = json.loads(
        block("json", '"selected_attempt_id"').replace("<64 lowercase hex characters>", "a" * 64)
    )
    assert set(example) <= set(RunRecord.model_fields), "contract field missing from the response"
    extras: dict[str, Any] = {
        "scenario": {"id": "invoice-429", "version": 1},
        "agent": {"id": "invoice-resilient", "version": "1.0.0"},
        "seed": "42",
        "labels": {},
    }
    import uuid
    from datetime import datetime

    record = RunRecord(
        **{
            **example,
            **extras,
            "id": uuid.UUID(example["id"]),
            "selected_attempt_id": uuid.UUID(example["selected_attempt_id"]),
            "attempts": [{**a, "id": uuid.UUID(a["id"])} for a in example["attempts"]],
            "created_at": datetime.fromisoformat(example["created_at"]),
            "completed_at": datetime.fromisoformat(example["completed_at"]),
        }
    )
    dumped = record.model_dump(mode="json")
    for key, value in example.items():
        assert dumped[key] == value, key


@pytest.mark.parametrize("marker", ['"schema_version":"1.0"'])
def test_event_example_is_not_yet_modelled(marker: str) -> None:
    """Events arrive in M2; this guards against silently claiming they are covered."""
    assert any(marker in text.replace(" ", "").replace("\n", "") for _, text in BLOCKS)
    assert not (ROOT / "src/agentcrash/domain/events.py").exists()
