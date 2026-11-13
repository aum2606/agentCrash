import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "openapi.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# Exactly the implemented slice of the CONTRACTS.md section 3 table: M1+M2, the M4 export,
# strict-replay and rerun routes, and experiment creation and progress. Everything else (the
# experiment report, artifact download) belongs to later slices and must not appear as a
# half-built route.
M1_ROUTES = {
    ("get", "/health/live"),
    ("get", "/health/ready"),
    ("get", "/api/v1/agents"),
    ("get", "/api/v1/scenarios"),
    ("post", "/api/v1/scenarios"),
    ("get", "/api/v1/scenarios/{scenario_id}/versions/{version}"),
    ("get", "/api/v1/runs"),
    ("post", "/api/v1/runs"),
    ("get", "/api/v1/runs/{run_id}"),
    ("post", "/api/v1/runs/{run_id}/cancel"),
    ("get", "/api/v1/runs/{run_id}/events"),
    ("get", "/api/v1/runs/{run_id}/evaluations"),
    ("post", "/api/v1/runs/{run_id}/reruns"),
    ("post", "/api/v1/experiments"),
    ("get", "/api/v1/experiments/{experiment_id}"),
    ("post", "/api/v1/runs/{run_id}/exports"),
    ("get", "/api/v1/exports/{export_id}"),
    ("post", "/api/v1/runs/{run_id}/replays"),
    ("get", "/api/v1/replays/{replay_id}"),
}


def operations(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(m, p): op for p, item in spec["paths"].items() for m, op in item.items()}


def test_documented_routes_are_exactly_the_implemented_scope(spec: dict[str, Any]) -> None:
    assert set(operations(spec)) == M1_ROUTES


def test_api_routes_declare_bearer_auth_and_health_does_not(spec: dict[str, Any]) -> None:
    assert spec["components"]["securitySchemes"]
    for (_, path), op in operations(spec).items():
        if path.startswith("/api/v1"):
            assert "401" in op["responses"], path
            assert op["security"], path
        else:
            assert "security" not in op, path


def test_error_responses_use_the_shared_envelope(spec: dict[str, Any]) -> None:
    assert "ErrorEnvelope" in spec["components"]["schemas"]
    assert "HTTPValidationError" not in json.dumps(
        spec
    )  # 422s are normalized, not framework-shaped
    for (_, path), op in operations(spec).items():
        for status, response in op["responses"].items():
            if status.startswith(("4", "5")):
                ref = response["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorEnvelope"), (path, status)


def test_run_creation_documents_the_idempotency_contract(spec: dict[str, Any]) -> None:
    op = operations(spec)[("post", "/api/v1/runs")]
    key = next(p for p in op["parameters"] if p["name"] == "Idempotency-Key")
    assert key["required"] is True and key["in"] == "header"
    assert key["schema"]["maxLength"] == 128
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/RunCreate"}
    assert set(op["responses"]) >= {"202", "400", "409", "410", "422", "429"}
    assert op["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/RunRecord"
    )


def test_scenario_creation_accepts_json_and_yaml(spec: dict[str, Any]) -> None:
    op = operations(spec)[("post", "/api/v1/scenarios")]
    assert set(op["requestBody"]["content"]) == {"application/json", "application/yaml"}
    assert set(op["responses"]) >= {"200", "201", "409", "415", "422"}


def test_request_schemas_reject_unknown_fields(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    for name in ("Scenario", "RunCreate", "FaultRule", "Budgets"):
        assert schemas[name]["additionalProperties"] is False, name


def test_pagination_parameters_are_documented(spec: dict[str, Any]) -> None:
    ops = operations(spec)
    names = {p["name"] for p in ops[("get", "/api/v1/runs")]["parameters"]}
    assert names == {"status", "scenario_id", "cursor", "limit"}
    names = {p["name"] for p in ops[("get", "/api/v1/scenarios")]["parameters"]}
    assert names == {"cursor", "limit"}
