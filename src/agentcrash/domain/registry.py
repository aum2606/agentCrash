"""Trusted registry of what scenarios may reference. Built by the composition root
(``agentcrash.bootstrap``); the compiler only reads it, so it never touches files or imports."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mutating: bool
    deployment_forbidden: bool  # may never be granted by a scenario
    supported_effects: frozenset[str]  # fault effect types this tool may be the target of
    # Successful response body as nested mappings (object) and None (leaf); lets the compiler
    # reject a schema_mutation pointer that names nothing. None: the tool is not mutable.
    body_shape: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GraderSpec:
    id: str
    version: int
    always_required: bool  # integrity/safety graders the compiler forces on every scenario
    params: Mapping[str, str]  # parameter name -> "rule_ref" | "tool_ref"


@dataclass(frozen=True)
class EnvironmentSpec:
    id: str
    version: int
    tasks: frozenset[str]
    actors: frozenset[str]
    fixtures: Mapping[str, str]  # fixture_id -> content hash
    tools: Mapping[str, ToolSpec]


@dataclass(frozen=True)
class AgentSpec:
    id: str
    version: str
    config_model: type[BaseModel]
    source_hash: str | None


@dataclass(frozen=True)
class Registry:
    environments: Mapping[tuple[str, int], EnvironmentSpec]
    graders: Mapping[str, GraderSpec]
    agents: Mapping[tuple[str, str], AgentSpec]
    # Repository-owned fault fixtures. Empty until M3 ships them, so rules that need them
    # fail closed with UNKNOWN_REFERENCE instead of compiling into a runtime no-op.
    stale_snapshots: Mapping[str, str] = field(default_factory=dict)  # snapshot_id -> tool name
    document_fixtures: frozenset[str] = frozenset()
    conflict_patches: frozenset[str] = frozenset()
