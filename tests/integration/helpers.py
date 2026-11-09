"""Shared helpers for integration tests."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

ROOT = Path(__file__).resolve().parents[2]
DATA_TABLES = (
    "api_idempotency, jobs, run_attempts, runs, experiments, agent_versions, scenario_versions"
)


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def migrate(engine: Engine, revision: str = "head") -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def run_concurrently(n: int, call: Callable[[int], Any]) -> list[Any]:
    """Release ``n`` callers at the same instant with a barrier (no sleeps)."""
    barrier = threading.Barrier(n)

    def worker(i: int) -> Any:
        barrier.wait(timeout=30)
        return call(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))
