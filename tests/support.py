"""Shared test doubles (importable as ``support``; tests/ is on the pytest/mypy path)."""

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agentcrash.domain.events import GENESIS, StoredEvent, next_hash, project
from agentcrash.domain.models import CompiledScenario, Manifest, SourceInfo, ToolchainInfo
from agentcrash.domain.ports import (
    CreateRunResult,
    PutScenarioResult,
    RunPage,
    RunRecord,
    ScenarioPage,
    StoredScenario,
)
from agentcrash.domain.registry import Registry
from agentcrash.domain.runtime import StoredCall
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.manifest import build_manifest, validate_run_create

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- a synthetic, validly chained attempt (no database), shared by the bundle and CLI tests -----

RUN = {
    "schema_version": "1.0",
    "scenario": {"id": "invoice-429", "version": 1},
    "agent": {"id": "invoice-resilient", "version": "1.0.0", "config": {}},
    "seed": "42",
    "mode": "mock",
    "budgets": {"wall_ms": 120000, "logical_ms": 60000, "tool_calls": 50, "steps": 20},
    "labels": {},
}
WHEN = datetime(2026, 1, 1, tzinfo=UTC)
OUTCOME = {"kind": "response", "status": 200, "body": {"x": 1}, "meta": {}}


def synthetic_manifest(registry: Registry, doc: dict[str, Any]) -> Manifest:
    compiled = compile_scenario(copy.deepcopy(doc), registry)
    return build_manifest(
        compiled,
        validate_run_create(RUN, registry),
        registry,
        source=SourceInfo(commit=None, dirty=True),
        toolchain=ToolchainInfo(python="3.12.13", lock_hash=None),
    )


def chained(note: str = "read") -> list[StoredEvent]:
    specs = [
        ("attempt.started", None, None, {}),
        ("agent.step", "agent", None, {"note": note}),
        ("tool.requested", "agent", "call-1", {"tool": "invoice.get", "args_hash": "a" * 64}),
        ("agent.finished", "agent", None, {"status": "completed", "summary": "done"}),
    ]
    out, prev = [], GENESIS
    for seq, (type_, actor, call, payload) in enumerate(specs, start=1):
        digest = next_hash(prev, project(seq, type_, 0, actor, call, payload))
        out.append(StoredEvent(seq, type_, 0, actor, call, payload, WHEN, prev, digest))
        prev = digest
    return out


CALLS = [StoredCall("call-1", 1, 1, "b" * 64, OUTCOME)]


class StubStore:
    """Fails loudly if the API reaches the store when it should have rejected the request."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _unexpected(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"unexpected store call: {name}")

    def ping(self) -> None:
        self._unexpected("ping")

    def schema_revision(self) -> str | None:
        return self._unexpected("schema_revision")  # type: ignore[no-any-return]

    def put_scenario(self, compiled: CompiledScenario) -> PutScenarioResult:
        return self._unexpected("put_scenario")  # type: ignore[no-any-return]

    def get_scenario(self, scenario_id: str, version: int) -> StoredScenario | None:
        return self._unexpected("get_scenario")  # type: ignore[no-any-return]

    def list_scenarios(self, cursor: str | None, limit: int) -> ScenarioPage:
        return self._unexpected("list_scenarios")  # type: ignore[no-any-return]

    def ensure_agent_version(self, *args: Any) -> None:
        self._unexpected("ensure_agent_version")

    def create_run(self, **kwargs: Any) -> CreateRunResult:
        return self._unexpected("create_run")  # type: ignore[no-any-return]

    def get_run_create(self, run_id: UUID) -> Any:
        return self._unexpected("get_run_create")

    def get_run(self, run_id: UUID) -> RunRecord | None:
        return self._unexpected("get_run")  # type: ignore[no-any-return]

    def list_runs(
        self, status: str | None, scenario_id: str | None, cursor: str | None, limit: int
    ) -> RunPage:
        return self._unexpected("list_runs")  # type: ignore[no-any-return]

    def cancel_run(self, run_id: UUID, reason: str | None) -> Any:
        return self._unexpected("cancel_run")

    def list_events(self, run_id: UUID, attempt_id: UUID | None, after_seq: int, limit: int) -> Any:
        return self._unexpected("list_events")

    def list_evaluations(self, run_id: UUID, attempt_id: UUID | None) -> Any:
        return self._unexpected("list_evaluations")

    def create_export(self, **kwargs: Any) -> Any:
        return self._unexpected("create_export")

    def get_export(self, export_id: UUID) -> Any:
        return self._unexpected("get_export")

    def create_replay(self, **kwargs: Any) -> Any:
        return self._unexpected("create_replay")

    def get_replay(self, replay_id: UUID) -> Any:
        return self._unexpected("get_replay")

    def create_experiment(self, **kwargs: Any) -> Any:
        return self._unexpected("create_experiment")

    def get_experiment(self, experiment_id: UUID) -> Any:
        return self._unexpected("get_experiment")

    def get_experiment_evidence(self, experiment_id: UUID) -> Any:
        return self._unexpected("get_experiment_evidence")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now
