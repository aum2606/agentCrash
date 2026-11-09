"""Shared test doubles (importable as ``support``; tests/ is on the pytest/mypy path)."""

from typing import Any
from uuid import UUID

from agentcrash.domain.models import CompiledScenario
from agentcrash.domain.ports import (
    CreateRunResult,
    PutScenarioResult,
    RunPage,
    RunRecord,
    ScenarioPage,
    StoredScenario,
)

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now
