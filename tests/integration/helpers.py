"""Shared helpers for integration tests."""

from pathlib import Path

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
