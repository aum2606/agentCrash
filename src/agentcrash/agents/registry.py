"""Allowlist of shipped agent adapters (ADR-009: no arbitrary adapter upload).

Specs (names, versions, config schemas) are what the API validates. ``ADAPTERS`` holds the
implementations that can run: the naive reference agent in M2, the resilient one in M3.
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from agentcrash.agents.naive import NaiveAgent
from agentcrash.domain.registry import AgentSpec
from agentcrash.domain.runtime import AgentAdapter


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AGENTS: dict[tuple[str, str], AgentSpec] = {
    (agent_id, "1.0.0"): AgentSpec(agent_id, "1.0.0", EmptyConfig, None)
    for agent_id in ("invoice-naive", "invoice-resilient")
}

# Adapters that can actually run. ``invoice-resilient`` is registered above but has no
# implementation until M3, so a run that references it ends in ``error`` (ADAPTER_NOT_IMPLEMENTED).
ADAPTERS: dict[tuple[str, str], Callable[[], AgentAdapter]] = {
    ("invoice-naive", "1.0.0"): NaiveAgent
}
