"""Allowlist of shipped agent adapters (ADR-009: no arbitrary adapter upload).

M1 knows only names, versions and config schemas. The deterministic reference adapters are
implemented in M2/M3; until then these entries can be referenced by runs but not executed.
"""

from pydantic import BaseModel, ConfigDict

from agentcrash.domain.registry import AgentSpec


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AGENTS: dict[tuple[str, str], AgentSpec] = {
    (agent_id, "1.0.0"): AgentSpec(agent_id, "1.0.0", EmptyConfig, None)
    for agent_id in ("invoice-naive", "invoice-resilient")
}
