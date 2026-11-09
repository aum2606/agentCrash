"""M1 acceptance: restarting the API retains a queued run (real processes, real PostgreSQL),
and `dev init` is idempotent and never overwrites the token."""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2 as httpx
from sqlalchemy import URL, Engine, text
from typer.testing import CliRunner

from agentcrash.bootstrap import dev_init
from agentcrash.cli.main import app as cli
from agentcrash.settings import Settings

from .helpers import ROOT

TOKEN = "r" * 43
SCENARIO_YAML = (ROOT / "scenarios" / "invoice" / "v1" / "invoice-429.yaml").read_bytes()
RUN = {
    "schema_version": "1.0",
    "scenario": {"id": "invoice-429", "version": 1},
    "agent": {"id": "invoice-resilient", "version": "1.0.0", "config": {}},
    "seed": "7",
    "mode": "mock",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def api_process(database_url: URL, token_file: Path) -> Iterator[str]:
    """A real `agentcrash api` process; yields its base URL once /health/ready is OK."""
    port = free_port()
    env = {
        **os.environ,
        "AGENTCRASH_ROOT": str(ROOT),
        "AGENTCRASH_TOKEN_FILE": str(token_file),
        "AGENTCRASH_DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "agentcrash.cli.main", "api", "--port", str(port)],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/health/ready", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert proc.poll() is None, "api process exited early"
            assert time.monotonic() < deadline, "api did not become ready"
            time.sleep(0.1)
        yield base
    finally:
        proc.kill()  # abrupt, like a crash: no graceful shutdown to hide lost state
        proc.wait(timeout=10)


def test_queued_run_survives_an_api_restart(
    database_url: URL, clean_engine: Engine, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    headers = {**auth, "Idempotency-Key": "restart-1"}

    with api_process(database_url, token_file) as base:
        created = httpx.post(
            f"{base}/api/v1/scenarios",
            content=SCENARIO_YAML,
            headers={**auth, "Content-Type": "application/yaml"},
        )
        assert created.status_code == 201
        first = httpx.post(f"{base}/api/v1/runs", content=json.dumps(RUN), headers=headers)
        assert first.status_code == 202
        run = first.json()
        assert run["status"] == "queued"
    # the first process was killed here

    with clean_engine.connect() as conn:  # durable in PostgreSQL, independent of any API process
        assert conn.execute(text("SELECT status FROM runs")).scalars().all() == ["queued"]

    with api_process(database_url, token_file) as base:
        again = httpx.get(f"{base}/api/v1/runs/{run['id']}", headers=auth)
        assert again.status_code == 200
        assert again.json() == run  # same run, still queued, nothing changed
        listed = httpx.get(f"{base}/api/v1/runs", headers=auth).json()["items"]
        assert [r["id"] for r in listed] == [run["id"]]
        replay = httpx.post(f"{base}/api/v1/runs", content=json.dumps(RUN), headers=headers)
        assert replay.status_code == 202 and replay.json() == run
        assert replay.headers["idempotent-replayed"] == "true"  # key survived the restart too

    with clean_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM run_attempts")).scalar() == 0


def test_dev_init_seeds_idempotently_and_never_overwrites_the_token(
    database_url: URL, clean_engine: Engine, tmp_path: Path
) -> None:
    settings = Settings(root=ROOT, database_url=database_url, token_path=tmp_path / "x" / "token")
    first = dev_init(settings)
    assert (first.token_created, first.scenarios_created, first.scenarios_unchanged) == (True, 1, 0)
    assert first.agents == 2
    token = settings.token_path.read_text(encoding="utf-8")
    assert len(token.strip()) >= 43

    second = dev_init(settings)
    assert (second.token_created, second.scenarios_created, second.scenarios_unchanged) == (
        False,
        0,
        1,
    )
    assert settings.token_path.read_text(encoding="utf-8") == token  # untouched
    with clean_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_versions")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM scenario_versions")).scalar() == 1


def test_dev_init_cli_reports_and_refuses_an_unmigrated_database(
    database_url: URL, clean_engine: Engine, tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AGENTCRASH_ROOT", str(ROOT))
        mp.setenv("AGENTCRASH_TOKEN_FILE", str(tmp_path / "token"))
        mp.setenv("AGENTCRASH_DATABASE_URL", database_url.render_as_string(hide_password=False))
        ok = CliRunner().invoke(cli, ["dev", "init"])
        assert ok.exit_code == 0, ok.output
        assert "token created" in ok.output and "scenarios created=1" in ok.output

        with clean_engine.begin() as conn:  # simulate a database that was never migrated
            conn.execute(text("UPDATE alembic_version SET version_num = '0000'"))
        try:
            bad = CliRunner().invoke(cli, ["dev", "init"])
            assert bad.exit_code == 2
            assert "alembic upgrade head" in bad.output
        finally:
            with clean_engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    finally:
        mp.undo()
