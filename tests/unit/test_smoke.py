import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentcrash import __version__
from agentcrash.api.app import Services, create_app
from agentcrash.cli.main import app as cli

ROOT = Path(__file__).resolve().parents[2]


def test_health_live_in_process(stub_services: Services) -> None:
    r = TestClient(create_app(stub_services)).get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


def test_cli_version() -> None:
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_api_refuses_non_loopback_host() -> None:
    result = CliRunner().invoke(cli, ["api", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2


def test_api_refuses_to_start_without_a_token(tmp_path: Path, monkeypatch: object) -> None:
    env = {**os.environ, "AGENTCRASH_TOKEN_FILE": str(tmp_path / "missing-token")}
    env["AGENTCRASH_DATABASE_URL"] = "postgresql+psycopg://u:p@127.0.0.1:1/db"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "agentcrash.cli.main", "api", "--port", "1"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "dev init" in result.stderr


def test_api_process_serves_health_live(tmp_path: Path) -> None:
    """Start the real `agentcrash api` process (real app factory) and hit it over HTTP.

    The database URL points at a closed port: liveness must not depend on the database."""
    token = tmp_path / "token"
    token.write_text("x" * 43, encoding="utf-8")
    env = {
        **os.environ,
        "AGENTCRASH_TOKEN_FILE": str(token),
        "AGENTCRASH_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/db",
    }
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "agentcrash.cli.main", "api", "--port", str(port)],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=1)
                break
            except httpx.TransportError:
                assert proc.poll() is None, "api process exited early"
                assert time.monotonic() < deadline, "api did not become ready"
                time.sleep(0.1)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        # Readiness does depend on the database and must say so, not crash or claim ready.
        ready = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=10)
        assert ready.status_code == 503
        assert ready.json()["error"]["code"] == "NOT_READY"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
