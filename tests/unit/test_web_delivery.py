"""The built web client is served from the API origin with a self-only CSP (ADR-010)."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcrash.api.app import Services, create_app
from agentcrash.api.web import CSP
from support import AUTH, TOKEN


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>AgentCrash Lab</title>", "utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", "utf-8")
    (tmp_path / "secret.txt").write_text("not for the web", "utf-8")
    return root


def client_for(services: Services, web_dir: Path | None) -> TestClient:
    return TestClient(create_app(replace(services, web_dir=web_dir)), raise_server_exceptions=False)


def test_page_and_assets_are_served_without_a_token(stub_services: Services, web_dir: Path) -> None:
    client = client_for(stub_services, web_dir)
    page = client.get("/")
    assert page.status_code == 200
    assert "AgentCrash Lab" in page.text
    assert page.headers["cache-control"] == "no-store"
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"


def test_page_never_contains_the_token(stub_services: Services, web_dir: Path) -> None:
    client = client_for(stub_services, web_dir)
    assert TOKEN not in client.get("/").text
    assert TOKEN not in client.get("/assets/app.js").text


def test_every_response_carries_the_security_headers(
    stub_services: Services, web_dir: Path
) -> None:
    client = client_for(stub_services, web_dir)
    for response in (
        client.get("/"),
        client.get("/assets/app.js"),
        client.get("/assets/missing.js"),
        client.get("/health/live"),
        client.get("/api/v1/runs"),  # 401: errors are covered too
        client.get("/api/v1/agents", headers=AUTH),
    ):
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_csp_allows_only_this_origin() -> None:
    directives = dict(d.strip().split(" ", 1) for d in CSP.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP and "http" not in CSP


def test_the_api_still_requires_the_token(stub_services: Services, web_dir: Path) -> None:
    client = client_for(stub_services, web_dir)
    assert client.get("/api/v1/runs").status_code == 401
    assert client.get("/api/v1/agents", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "path", ["/assets/../secret.txt", "/assets/%2e%2e/secret.txt", "/assets/..%2fsecret.txt"]
)
def test_assets_cannot_escape_their_directory(
    stub_services: Services, web_dir: Path, path: str
) -> None:
    response = client_for(stub_services, web_dir).get(path)
    assert response.status_code in (400, 404)
    assert "not for the web" not in response.text


def test_nothing_is_served_until_the_client_is_built(
    stub_services: Services, tmp_path: Path
) -> None:
    for missing in (None, tmp_path / "nowhere"):
        client = client_for(stub_services, missing)
        assert client.get("/").status_code == 404
        assert client.get("/assets/app.js").status_code == 404
        assert client.get("/health/live").status_code == 200
