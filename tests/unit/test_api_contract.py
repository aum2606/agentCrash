"""API behavior that must hold without touching the database: auth, envelope, input limits,
header rules and rate limiting. The stub store fails the test if a rejected request reaches it."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentcrash.api.app import Services, create_app
from support import AUTH, FakeClock, StubStore

RUN_BODY = {
    "schema_version": "1.0",
    "scenario": {"id": "invoice-429", "version": 1},
    "agent": {"id": "invoice-resilient", "version": "1.0.0", "config": {}},
    "seed": "42",
    "mode": "mock",
}
KEY = {"Idempotency-Key": "k-1"}


@pytest.fixture
def client(stub_services: Services) -> TestClient:
    return TestClient(create_app(stub_services), raise_server_exceptions=False)


def error_of(response: Any) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert set(err) == {"code", "message", "details", "request_id", "retryable"}
    assert err["request_id"] == response.headers["x-request-id"]
    assert isinstance(err["retryable"], bool)
    return err  # type: ignore[no-any-return]


# --- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic " + "t" * 43},
        {"Authorization": "Bearer"},
        {"Authorization": "bearer " + "t" * 42},  # one character short
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/v1/agents"), ("GET", "/api/v1/runs"), ("POST", "/api/v1/runs")],
)
def test_every_api_route_requires_the_token(
    client: TestClient, stub_store: StubStore, headers: dict[str, str], method: str, path: str
) -> None:
    r = client.request(method, path, headers=headers, content=b"{}")
    assert r.status_code == 401
    assert error_of(r)["code"] == "UNAUTHENTICATED"
    assert r.headers["www-authenticate"] == "Bearer"
    assert stub_store.calls == []


def test_auth_is_checked_before_the_body_is_read_or_validated(client: TestClient) -> None:
    r = client.post(
        "/api/v1/scenarios", content=b"\xff not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 401


def test_valid_token_is_accepted_case_insensitive_scheme(client: TestClient) -> None:
    r = client.get(
        "/api/v1/agents",
        headers={"Authorization": AUTH["Authorization"].replace("Bearer", "bearer")},
    )
    assert r.status_code == 200


def test_liveness_needs_no_token_and_leaks_nothing(client: TestClient) -> None:
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- envelope --------------------------------------------------------------------------------


def test_unknown_route_and_method_use_the_envelope(client: TestClient) -> None:
    assert error_of(client.get("/nope"))["code"] == "NOT_FOUND"
    r = client.put("/api/v1/agents", headers=AUTH)
    assert r.status_code == 405 and error_of(r)["code"] == "METHOD_NOT_ALLOWED"


def test_each_response_gets_a_fresh_server_generated_request_id(client: TestClient) -> None:
    ids = {
        client.get("/health/live", headers={"X-Request-ID": "attacker-chosen"}).headers[
            "x-request-id"
        ]
        for _ in range(3)
    }
    assert len(ids) == 3 and "attacker-chosen" not in ids


def test_unhandled_errors_are_generic_and_do_not_leak(
    stub_services: Services, stub_store: StubStore
) -> None:
    class Boom(StubStore):
        def list_runs(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("SELECT secret FROM runs -- password=hunter2")

    from dataclasses import replace

    c = TestClient(create_app(replace(stub_services, store=Boom())), raise_server_exceptions=False)
    r = c.get("/api/v1/runs", headers=AUTH)
    assert r.status_code == 500
    assert error_of(r)["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in r.text and "SELECT" not in r.text and "Traceback" not in r.text


def test_readiness_reports_503_without_detail_when_database_is_down(
    stub_services: Services,
) -> None:
    from dataclasses import replace

    class Down(StubStore):
        def ping(self) -> None:
            raise ConnectionError("could not connect to 10.0.0.5 as agentcrash/pw")

    c = TestClient(create_app(replace(stub_services, store=Down())))
    r = c.get("/health/ready")
    assert r.status_code == 503 and error_of(r)["code"] == "NOT_READY"
    assert error_of(r)["retryable"] is True
    assert "10.0.0.5" not in r.text


def test_readiness_requires_the_expected_schema_revision(stub_services: Services) -> None:
    from dataclasses import replace

    class Old(StubStore):
        def ping(self) -> None:
            return None

        def schema_revision(self) -> str | None:
            return None  # never migrated

    c = TestClient(create_app(replace(stub_services, store=Old())))
    assert c.get("/health/ready").status_code == 503


# --- scenarios -------------------------------------------------------------------------------


def post_scenario(
    client: TestClient, body: bytes, content_type: str | None = "application/json"
) -> Any:
    headers = dict(AUTH)
    if content_type:
        headers["Content-Type"] = content_type
    return client.post("/api/v1/scenarios", content=body, headers=headers)


def test_scenario_media_type_must_be_json_or_yaml(
    client: TestClient, stub_store: StubStore
) -> None:
    assert post_scenario(client, b"{}", "text/plain").status_code == 415
    assert post_scenario(client, b"{}", None).status_code == 415
    assert stub_store.calls == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{not json", "INVALID_JSON"),
        (b"[]", "NOT_AN_OBJECT"),
        (b'{"a": 1, "a": 2}', "DUPLICATE_KEY"),
        (b'{"a": 1.5}', "FLOAT_NOT_ALLOWED"),
        (b'{"a": NaN}', "FLOAT_NOT_ALLOWED"),
        (b"[" * 5000, "DOCUMENT_TOO_DEEP"),
        (b'{"a":' * 100 + b"1" + b"}" * 100, "DOCUMENT_TOO_DEEP"),
        (b"\xff\xfe", "INVALID_ENCODING"),
    ],
)
def test_malformed_scenario_json_is_a_422_with_a_stable_code(
    client: TestClient, stub_store: StubStore, body: bytes, code: str
) -> None:
    r = post_scenario(client, body)
    assert r.status_code == 422
    err = error_of(r)
    assert err["code"] == "SCENARIO_VALIDATION_FAILED"
    assert err["details"][0]["code"] == code
    assert stub_store.calls == []


def test_invalid_scenario_reports_paths_and_codes_not_content(
    client: TestClient, stub_store: StubStore, doc: dict[str, Any]
) -> None:
    doc["faults"][0]["probability"] = "IGNORE PREVIOUS INSTRUCTIONS"
    doc["surprise"] = 1
    r = post_scenario(client, json.dumps(doc).encode())
    err = error_of(r)
    assert {(d["path"], d["code"]) for d in err["details"]} == {
        ("/faults/0/probability", "INVALID_PROBABILITY"),
        ("/surprise", "UNKNOWN_FIELD"),
    }
    assert all(set(d) == {"path", "code"} for d in err["details"])
    assert "IGNORE" not in r.text
    assert stub_store.calls == []


def test_yaml_tags_are_rejected_over_http_too(client: TestClient, stub_store: StubStore) -> None:
    r = post_scenario(
        client, b"x: !!python/object/apply:os.system ['echo hi']\n", "application/yaml"
    )
    assert r.status_code == 422 and error_of(r)["details"][0]["code"] == "YAML_TAG_FORBIDDEN"
    assert stub_store.calls == []


def test_oversized_bodies_are_rejected_by_declared_and_streamed_size(
    client: TestClient, stub_store: StubStore
) -> None:
    big = b"{" + b" " * (1 << 20) + b"}"
    r = post_scenario(client, big)
    assert r.status_code == 413 and error_of(r)["code"] == "PAYLOAD_TOO_LARGE"

    def chunks() -> Any:  # no Content-Length: chunked upload must be capped while streaming
        for _ in range(3):
            yield b" " * (1 << 19)

    r = client.post(
        "/api/v1/scenarios", content=chunks(), headers={**AUTH, "Content-Type": "application/json"}
    )
    assert r.status_code == 413
    assert stub_store.calls == []


# --- runs: headers, validation, rate limit ---------------------------------------------------


def post_run(client: TestClient, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    payload = json.dumps(RUN_BODY if body is None else body).encode()
    return client.post(
        "/api/v1/runs",
        content=payload,
        headers={**AUTH, **(headers if headers is not None else KEY)},
    )


def test_idempotency_key_is_required_and_bounded(client: TestClient, stub_store: StubStore) -> None:
    r = post_run(client, headers={})
    assert r.status_code == 400 and error_of(r)["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    for bad in (b"k" * 129, b"", "k\u00e9y".encode(), b"tab	here"):  # raw bytes, as on the wire
        r = client.post(
            "/api/v1/runs",
            content=json.dumps(RUN_BODY).encode(),
            headers=[(b"authorization", AUTH["Authorization"].encode()), (b"idempotency-key", bad)],
        )
        assert r.status_code == 400, repr(bad)
        assert error_of(r)["code"] == "IDEMPOTENCY_KEY_INVALID"
    assert stub_store.calls == []


@pytest.mark.parametrize(
    ("change", "path", "code"),
    [
        ({"agent": {"id": "invoice-evil", "version": "1.0.0"}}, "/agent/id", "UNKNOWN_ADAPTER"),
        ({"seed": "-1"}, "/seed", "INVALID_SEED"),
        ({"mode": "live"}, "/mode", "UNSUPPORTED_MODE"),
        ({"budgets": {"tool_calls": 51}}, "/budgets/tool_calls", "BUDGET_EXCEEDS_CAP"),
        ({"api_key": "sk-secret"}, "/api_key", "UNKNOWN_FIELD"),
    ],
)
def test_invalid_runs_are_422_before_any_store_call(
    client: TestClient, stub_store: StubStore, change: dict[str, Any], path: str, code: str
) -> None:
    r = post_run(client, {**RUN_BODY, **change})
    assert r.status_code == 422
    err = error_of(r)
    assert err["code"] == "RUN_VALIDATION_FAILED"
    assert {"path": path, "code": code} in err["details"]
    assert "sk-secret" not in r.text
    assert stub_store.calls == []


def test_run_creation_is_rate_limited_per_minute(
    client: TestClient, clock: FakeClock, stub_store: StubStore
) -> None:
    for _ in range(10):  # allowed through the limiter; rejected by validation, so no store call
        assert post_run(client, {**RUN_BODY, "mode": "live"}).status_code == 422
    r = post_run(client, {**RUN_BODY, "mode": "live"})
    assert r.status_code == 429
    err = error_of(r)
    assert err["code"] == "RATE_LIMITED" and err["retryable"] is True
    assert int(r.headers["retry-after"]) >= 1
    clock.now += 61
    assert post_run(client, {**RUN_BODY, "mode": "live"}).status_code == 422
    assert stub_store.calls == []


# --- list endpoints --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "?bogus=1",
        "?status=exploded",
        "?limit=0",
        "?limit=201",
        "?limit=abc",
        "?limit=-1",
        "?limit=1.5",
    ],
)
def test_bad_list_parameters_are_400(client: TestClient, stub_store: StubStore, query: str) -> None:
    r = client.get("/api/v1/runs" + query, headers=AUTH)
    assert r.status_code == 400
    assert error_of(r)["code"] in {"UNSUPPORTED_FILTER", "INVALID_PARAMETER"}
    assert stub_store.calls == []


def test_unknown_run_ids_are_404_without_touching_the_store_for_garbage(
    client: TestClient, stub_store: StubStore
) -> None:
    r = client.get("/api/v1/runs/not-a-uuid", headers=AUTH)
    assert r.status_code == 404 and error_of(r)["code"] == "RUN_NOT_FOUND"
    assert client.get("/api/v1/scenarios/s/versions/abc", headers=AUTH).status_code == 404
    assert stub_store.calls == []


def test_agents_endpoint_lists_the_allowlist_with_config_schemas(client: TestClient) -> None:
    items = client.get("/api/v1/agents", headers=AUTH).json()["items"]
    assert [(i["id"], i["version"]) for i in items] == [
        ("invoice-naive", "1.0.0"),
        ("invoice-resilient", "1.0.0"),
    ]
    assert items[0]["config_schema"]["additionalProperties"] is False


def test_api_modules_do_not_expose_interactive_docs(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
