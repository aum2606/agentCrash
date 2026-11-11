"""Shared helpers for integration tests."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from agentcrash.bootstrap import default_registry
from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.models import SourceInfo, ToolchainInfo
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.scenarios.manifest import build_manifest, manifest_hash, validate_run_create

if TYPE_CHECKING:
    from agentcrash.storage.store import PostgresStore

ROOT = Path(__file__).resolve().parents[2]
DATA_TABLES = (
    "evaluations, ledger_entries, command_receipts, environment_rows, tool_calls, events,"
    " api_idempotency, jobs, run_attempts, runs, experiments, agent_versions, scenario_versions"
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


# --- execution helpers (M2) -------------------------------------------------------------------


def submit_run(
    store: "PostgresStore",
    scenario: str = "invoice-healthy",
    agent: str = "invoice-naive",
    seed: str = "42",
    budgets: dict[str, int] | None = None,
) -> UUID:
    """Accept a run through the same domain path the API uses (no HTTP), return its id."""
    registry = default_registry(ROOT)
    (path,) = (ROOT / "scenarios" / "invoice" / "v1").rglob(f"{scenario}.yaml")
    doc = load_scenario_document(path.read_bytes())
    compiled = compile_scenario(doc, registry)
    store.put_scenario(compiled)
    for spec in registry.agents.values():
        store.ensure_agent_version(spec.id, spec.version, spec.id, "h", spec.source_hash)
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "scenario": {"id": scenario, "version": 1},
        "agent": {"id": agent, "version": "1.0.0", "config": {}},
        "seed": seed,
        "mode": "mock",
    }
    if budgets:
        body["budgets"] = budgets
    run = validate_run_create(body, registry)
    manifest = build_manifest(
        compiled,
        run,
        registry,
        source=SourceInfo(commit=None, dirty=True),
        toolchain=ToolchainInfo(python="3.12.13", lock_hash=None),
    )
    result = store.create_run(
        principal="test",
        route="POST /api/v1/runs",
        key=uuid4().hex,
        request_hash=canonical_hash(run.model_dump(mode="json")),
        run=run,
        manifest=manifest,
        manifest_hash=manifest_hash(manifest),
        queue_cap=100,
    )
    return result.run.id
