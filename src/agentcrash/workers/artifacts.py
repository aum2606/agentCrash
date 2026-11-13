"""Export and strict-replay jobs (M4). Both read a closed attempt's stored evidence and write
only the export/replay result: no gateway, no environment state, no network.

Replay runs the installed, allowlisted reference adapter in this worker process against the
recorded transcript (see ``agentcrash.replay``); it can never execute bundled or uploaded code.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from agentcrash.agents.rng import SeededRng
from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.ports import SubjectClaim
from agentcrash.domain.runtime import AgentAdapter
from agentcrash.replay.bundle import BundleError, build_bundle, write_bundle
from agentcrash.replay.engine import NotReplayable
from agentcrash.replay.service import Incompatible, replay_recorded
from agentcrash.storage.attempts import AttemptRepo
from agentcrash.storage.exports import ExportRepo

log = logging.getLogger(__name__)


@dataclass
class ArtifactJobs:
    repo: AttemptRepo
    exports: ExportRepo
    artifact_dir: Path
    adapters: Mapping[tuple[str, str], Callable[[], AgentAdapter]]

    def run(self, kind: str, claim: SubjectClaim) -> str:
        try:
            return self._export(claim) if kind == "export" else self._replay(claim)
        except Exception:
            # Leave the lease to expire; the reaper re-queues it (bounded by delivery count).
            log.exception("%s job %s failed", kind, claim.job_id)
            return "retry"

    def _export(self, claim: SubjectClaim) -> str:
        manifest = self.repo.manifest(claim.run_id)
        evidence = self.repo.evidence(claim.attempt_id, manifest.budgets, {})
        try:
            data = build_bundle(
                manifest,
                evidence.events,
                evidence.tool_calls,
                self.exports.evaluations(claim.attempt_id),
            )
        except BundleError as err:
            self.exports.fail_export(claim.job_id, claim.subject_id, err.code)
            return err.code
        # The file is durable before the metadata that points at it is published (ADR-012).
        path = write_bundle(self.artifact_dir, data)
        self.exports.complete_export(
            claim.job_id, claim.subject_id, path.stem, len(data), path.name
        )
        return "completed"

    def _replay(self, claim: SubjectClaim) -> str:
        manifest = self.repo.manifest(claim.run_id)
        evidence = self.repo.evidence(claim.attempt_id, manifest.budgets, {})
        try:
            result = replay_recorded(
                manifest, evidence.events, evidence.tool_calls, self.adapters, SeededRng
            )
        except Incompatible as err:
            code = err.reasons[0]
        except NotReplayable:
            code = "NOT_REPLAYABLE"
        else:
            adapter_hash = canonical_hash(
                {
                    "id": manifest.agent.id,
                    "version": manifest.agent.version,
                    "source_hash": manifest.agent.source_hash,
                }
            )
            self.exports.complete_replay(
                claim.job_id,
                claim.subject_id,
                adapter_hash,
                result.consumed,
                result.total,
                None if result.divergence is None else asdict(result.divergence),
            )
            return "completed" if result.ok else "diverged"
        self.exports.fail_replay(claim.job_id, claim.subject_id, code)
        return code
