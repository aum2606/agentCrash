"""Replay preconditions and the one entry point that ties them to the engine.

Strict replay is refused, with stable reasons, unless the recording is complete (``full``), an
allowlisted adapter of the *same id and version* is installed, and the engine versions recorded in
the manifest are the ones running now (ENGINEERING_PLAN.md section 5). A bundle never supplies
code: the adapter always comes from the installed allowlist.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence

from agentcrash.domain.canonical import CANONICALIZATION_VERSION
from agentcrash.domain.events import StoredEvent
from agentcrash.domain.models import (
    FAULT_ENGINE_VERSION,
    TAXONOMY_VERSION,
    Manifest,
)
from agentcrash.domain.runtime import AgentAdapter, AgentRng, StoredCall
from agentcrash.domain.task import task_from_manifest
from agentcrash.replay.engine import ReplayResult, build_transcript, strict_replay

Adapters = Mapping[tuple[str, str], Callable[[], AgentAdapter]]


class Incompatible(Exception):
    """Strict replay is not possible; ``reasons`` are stable codes."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def check_replayable(manifest: Manifest, adapters: Adapters) -> list[str]:
    reasons: list[str] = []
    if manifest.replay_capability != "full":
        reasons.append(f"REPLAY_CAPABILITY_{manifest.replay_capability.upper()}")
    if (manifest.agent.id, manifest.agent.version) not in adapters:
        reasons.append("ADAPTER_VERSION_UNAVAILABLE")
    for name, recorded, current in (
        ("canonicalization", manifest.canonicalization_version, CANONICALIZATION_VERSION),
        ("fault_engine", manifest.fault_engine_version, FAULT_ENGINE_VERSION),
        ("taxonomy", manifest.taxonomy_version, TAXONOMY_VERSION),
    ):
        if recorded != current:
            reasons.append(f"VERSION_INCOMPATIBLE_{name.upper()}")
    return reasons


def replay_recorded(
    manifest: Manifest,
    events: Sequence[StoredEvent],
    calls: Sequence[StoredCall],
    adapters: Adapters,
    rng_factory: Callable[[str, str], AgentRng],
) -> ReplayResult:
    """Strictly replay one recorded attempt. Raises ``Incompatible`` or ``NotReplayable``."""
    reasons = check_replayable(manifest, adapters)
    if reasons:
        raise Incompatible(reasons)
    transcript = build_transcript(events, calls)
    task = task_from_manifest(manifest)
    factory = adapters[(manifest.agent.id, manifest.agent.version)]
    rng = rng_factory(manifest.seed, manifest.agent.id)
    return asyncio.run(strict_replay(factory, rng, task, transcript))
