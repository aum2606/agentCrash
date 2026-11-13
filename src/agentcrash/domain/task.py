"""The task an agent is told, derived from the immutable manifest.

One function for both live execution and strict replay, so a replayed agent is handed exactly
the task the recorded one saw.
"""

from agentcrash.domain.models import Manifest
from agentcrash.domain.runtime import AgentTask

INSTRUCTION = (
    "Post invoice {invoice_id} against its current approved purchase order, once only. Do not "
    "change vendor details. Use current source data and preserve the invoice amount and "
    "currency. Purchase-order data must be no more than {age} milliseconds old relative to the "
    "supplied logical task time. Report inability rather than guessing."
)


def task_from_manifest(manifest: Manifest) -> AgentTask:
    task_input = manifest.scenario.task.input
    return AgentTask(
        task_id=manifest.scenario.task.id,
        instruction=INSTRUCTION.format(
            invoice_id=task_input.invoice_id, age=task_input.max_source_age_ms
        ),
        invoice_id=task_input.invoice_id,
        max_source_age_ms=task_input.max_source_age_ms,
        logical_epoch=manifest.logical_epoch,
        seed=manifest.seed,
    )
