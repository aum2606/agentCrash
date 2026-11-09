import copy
from pathlib import Path
from typing import Any

import pytest

from agentcrash.api.app import Services
from agentcrash.bootstrap import default_registry
from agentcrash.domain.models import SourceInfo, ToolchainInfo
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.loader import load_scenario_document
from support import TOKEN, FakeClock, StubStore

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_429 = ROOT / "scenarios" / "invoice" / "v1" / "invoice-429.yaml"


@pytest.fixture(scope="session")
def registry() -> Registry:
    return default_registry(ROOT)


@pytest.fixture
def doc() -> dict[str, Any]:
    """A fresh copy of the supplied invoice-429 scenario, safe to mutate."""
    return copy.deepcopy(load_scenario_document(SCENARIO_429.read_bytes()))


@pytest.fixture
def stub_store() -> StubStore:
    return StubStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stub_services(registry: Registry, stub_store: StubStore, clock: FakeClock) -> Services:
    return Services(
        store=stub_store,
        registry=registry,
        token=TOKEN,
        expected_revision="0001",
        provenance=lambda: (
            SourceInfo(commit=None, dirty=True),
            ToolchainInfo(python="3.12", lock_hash=None),
        ),
        clock=clock,
    )
