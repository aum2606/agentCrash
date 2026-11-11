"""The worker loop: reap, claim an execution or grading job, repeat.

Execution and grading use separate claim paths. Grading is idempotent by ``(attempt, grader
set)``, so a duplicated or retried grading job publishes at most one evaluation.
"""

import logging
import threading
from dataclasses import dataclass

from agentcrash.domain.evidence import verdict_of
from agentcrash.domain.ports import GradeClaim
from agentcrash.evaluation.classify import classify
from agentcrash.evaluation.graders import evaluate
from agentcrash.storage.attempts import AttemptRepo
from agentcrash.storage.queue import JobQueue
from agentcrash.workers.supervisor import AttemptSupervisor

log = logging.getLogger(__name__)


@dataclass
class Worker:
    queue: JobQueue
    repo: AttemptRepo
    supervisor: AttemptSupervisor
    owner: str
    poll_s: float = 0.5

    def grade(self, claim: GradeClaim) -> str:
        manifest = self.repo.manifest(claim.run_id)
        evidence = self.repo.evidence(claim.attempt_id, manifest.budgets, {})
        grades = evaluate(evidence, manifest.graders)
        verdict = verdict_of(grades, evidence.termination)
        primary, findings = classify(evidence, grades, verdict)
        self.repo.store_evaluation(
            claim.run_id,
            claim.attempt_id,
            manifest.grader_set_hash,
            evidence.final_state_hash,
            verdict,
            grades,
            claim.job_id,
            primary,
            findings,
        )
        return verdict

    def run_once(self) -> bool:
        """One pass. True if it did any work (so callers can poll without sleeping)."""
        did = bool(self.queue.reap_expired())
        claim = self.queue.claim_execute(self.owner)
        if claim is not None:
            log.info("attempt %s: %s", claim.attempt_id, self.supervisor.run_attempt(claim))
            return True
        graded = self.queue.claim_grade(self.owner)
        if graded is not None:
            try:
                log.info("graded %s: %s", graded.attempt_id, self.grade(graded))
            except Exception:
                # Leave the lease to expire; the reaper re-queues it (bounded by delivery count).
                log.exception("grading failed for attempt %s", graded.attempt_id)
            return True
        return did

    def run_forever(self, stop: threading.Event) -> None:
        self.supervisor.shutdown = stop
        while not stop.is_set():
            if not self.run_once():
                stop.wait(self.poll_s)
