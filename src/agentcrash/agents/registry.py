"""Allowlist of shipped agent adapters (ADR-009: no arbitrary adapter upload).

Specs (names, versions, config schemas) are what the API validates. ``ADAPTERS`` holds the
implementations that can run: the naive (M2) and resilient (M3) reference agents.
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from agentcrash.agents.naive import NaiveAgent
from agentcrash.agents.resilient import ResilientAgent
from agentcrash.domain.registry import AgentSpec
from agentcrash.domain.runtime import AgentAdapter


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AGENTS: dict[tuple[str, str], AgentSpec] = {
    (agent_id, "1.0.0"): AgentSpec(agent_id, "1.0.0", EmptyConfig, None)
    for agent_id in ("invoice-naive", "invoice-resilient")
}

# Adapters that can actually run. A spec without an entry here still validates but a run that
# references it ends in ``error`` (ADAPTER_NOT_IMPLEMENTED), never as a fake pass.
ADAPTERS: dict[tuple[str, str], Callable[[], AgentAdapter]] = {
    ("invoice-naive", "1.0.0"): NaiveAgent,
    ("invoice-resilient", "1.0.0"): ResilientAgent,
}
