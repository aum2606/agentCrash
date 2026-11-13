"""Experiments in PostgreSQL (CONTRACTS.md sections 3 and 8). Creation writes the idempotency
record, the experiment, every child run with its execute job, and every slot in one transaction
after a whole-batch queue-cap check: either the entire matrix exists or none of it does."""

from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, select

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.models import ExperimentCreate
from agentcrash.domain.ports import (
    CreateExperimentResult,
    ExperimentRecord,
    ExperimentSlot,
    PlannedRun,
    RoleProgress,
)
from agentcrash.storage import schema as t
from agentcrash.storage.admission import admit, insert_run
from agentcrash.storage.exports import _claim

Role = Literal["baseline", "candidate"]
Status = Literal["running", "completed", "error"]
ROLES: tuple[Role, ...] = ("baseline", "candidate")


def _progress(statuses: list[str]) -> RoleProgress:
    def n(status: str) -> int:
        return sum(s == status for s in statuses)

    return RoleProgress(
        total=len(statuses),
        queued=n("queued"),
        running=n("running"),
        evaluating=n("evaluating"),
        completed=n("completed"),
        error=n("error"),
    )


def _required(conn: Connection, experiment_id: UUID) -> ExperimentRecord:
    found = _load(conn, experiment_id)
    if found is None:
        raise LookupError(f"experiment {experiment_id} vanished inside its own transaction")
    return found


def _load(conn: Connection, experiment_id: UUID) -> ExperimentRecord | None:
    exp = conn.execute(select(t.experiments).where(t.experiments.c.id == experiment_id)).first()
    if exp is None:
        return None
    s, r = t.experiment_slots, t.runs
    rows = conn.execute(
        select(s, r.c.status, r.c.verdict)
        .join(r, r.c.id == s.c.run_id)
        .where(s.c.experiment_id == experiment_id)
        .order_by(s.c.scenario_id, s.c.scenario_version, s.c.seed, s.c.trial_index, s.c.agent_role)
    ).all()
    slots = [
        ExperimentSlot(
            scenario_id=x.scenario_id,
            scenario_version=x.scenario_version,
            seed=str(int(x.seed)),
            trial=x.trial_index,
            role=x.agent_role,
            run_id=x.run_id,
            status=x.status,
            verdict=x.verdict,
        )
        for x in rows
    ]
    statuses = [x.status for x in rows]
    status: Status = "running"
    if all(st in ("completed", "error") for st in statuses):
        status = "error" if "error" in statuses else "completed"
    config = ExperimentCreate.model_validate(exp.config_json)
    return ExperimentRecord(
        id=exp.id,
        name=config.name,
        gate_policy_id=config.gate_policy_id,
        config_hash=exp.config_hash,
        status=status,
        expected_runs=len(slots),
        progress={
            role: _progress([x.status for x in rows if x.agent_role == role]) for role in ROLES
        },
        slots=slots,
        created_at=exp.created_at,
    )


class ExperimentStoreMixin:
    _engine: Engine

    def create_experiment(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        config: ExperimentCreate,
        planned: list[PlannedRun],
        queue_cap: int,
    ) -> CreateExperimentResult:
        experiment_id = uuid4()
        with self._engine.begin() as conn:
            prior = _claim(conn, principal, route, key, request_hash, "experiment", experiment_id)
            if prior is not None:
                return CreateExperimentResult(experiment=_required(conn, prior), replayed=True)
            admit(conn, len(planned), queue_cap)  # the whole matrix fits, or none of it is written
            conn.execute(
                t.experiments.insert().values(
                    id=experiment_id,
                    config_json=config.model_dump(mode="json"),
                    config_hash=canonical_hash(config.model_dump(mode="json")),
                    status="created",
                )
            )
            for item in planned:
                run_id = uuid4()
                insert_run(
                    conn, run_id, item.run, item.manifest, item.manifest_hash, None, experiment_id
                )
                conn.execute(
                    t.experiment_slots.insert().values(
                        experiment_id=experiment_id,
                        scenario_hash=item.scenario_hash,
                        scenario_id=item.run.scenario.id,
                        scenario_version=item.run.scenario.version,
                        seed=int(item.run.seed),
                        trial_index=item.trial,
                        agent_role=item.role,
                        run_id=run_id,
                    )
                )
            return CreateExperimentResult(experiment=_required(conn, experiment_id), replayed=False)

    def get_experiment(self, experiment_id: UUID) -> ExperimentRecord | None:
        with self._engine.connect() as conn:
            return _load(conn, experiment_id)
