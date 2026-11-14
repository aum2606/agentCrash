"""Experiments in PostgreSQL (CONTRACTS.md sections 3 and 8). Creation writes the idempotency
record, the experiment, every child run with its execute job, and every slot in one transaction
after a whole-batch queue-cap check: either the entire matrix exists or none of it does."""

from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, and_, extract, func, select

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.models import ExperimentCreate
from agentcrash.domain.ports import (
    CreateExperimentResult,
    ExperimentEvidence,
    ExperimentRecord,
    ExperimentSlot,
    PlannedRun,
    RoleProgress,
    SlotEvidence,
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


def _evidence(conn: Connection, experiment_id: UUID) -> list[SlotEvidence]:
    s, r, a, ev, ex = t.experiment_slots, t.runs, t.run_attempts, t.events, t.evaluations
    rows = conn.execute(
        select(s, r.c.status, r.c.error_code, r.c.cancel_requested, r.c.verdict, r.c.manifest)
        .add_columns(r.c.manifest_hash, r.c.selected_attempt_id)
        .join(r, r.c.id == s.c.run_id)
        .where(s.c.experiment_id == experiment_id)
        .order_by(s.c.scenario_id, s.c.scenario_version, s.c.seed, s.c.trial_index, s.c.agent_role)
    ).all()
    attempt_ids = [x.selected_attempt_id for x in rows if x.selected_attempt_id is not None]
    counted = {
        x.attempt_id: x
        for x in conn.execute(
            select(
                ev.c.attempt_id,
                func.count().filter(ev.c.type == "agent.step").label("steps"),
                func.count().filter(ev.c.type == "tool.requested").label("calls"),
                func.count()
                .filter(and_(ev.c.type == "fault.decided", ev.c.payload["applied"].as_boolean()))
                .label("faults"),
                func.max(ev.c.logical_time_ms).label("logical"),
            )
            .where(ev.c.attempt_id.in_(attempt_ids))
            .group_by(ev.c.attempt_id)
        )
    }
    wall = {
        x.id: x.ms
        for x in conn.execute(
            select(
                a.c.id, (extract("epoch", a.c.closed_at - a.c.created_at) * 1000).label("ms")
            ).where(a.c.id.in_(attempt_ids), a.c.closed_at.is_not(None))
        )
    }
    evaluated = {}  # latest evaluation per attempt
    for x in conn.execute(
        select(ex).where(ex.c.attempt_id.in_(attempt_ids)).order_by(ex.c.evaluated_at)
    ):
        evaluated[x.attempt_id] = x
    out: list[SlotEvidence] = []
    for x in rows:
        c = counted.get(x.selected_attempt_id)
        e = evaluated.get(x.selected_attempt_id)
        recovery = next(
            (
                g["result"]
                for g in (e.grades if e else [])
                if g["grader_id"] == "recovery.observed.v1"
            ),
            None,
        )
        m = x.manifest
        out.append(
            SlotEvidence(
                scenario_id=x.scenario_id,
                scenario_version=x.scenario_version,
                seed=str(int(x.seed)),
                trial=x.trial_index,
                role=x.agent_role,
                run_id=x.run_id,
                manifest_hash=x.manifest_hash,
                scenario_hash=m["scenario_hash"],
                grader_set_hash=m["grader_set_hash"],
                versions=(
                    m["canonicalization_version"],
                    m["fault_engine_version"],
                    m["taxonomy_version"],
                ),
                run_status=x.status,
                error_code=x.error_code,
                cancelled=x.cancel_requested,
                verdict=x.verdict,
                primary_reason=e.primary_reason if e else None,
                finding_codes=tuple(f["code"] for f in (e.findings if e else [])),
                recovery=recovery,
                fault_applications=c.faults if c else 0,
                tool_calls=c.calls if c else None,
                steps=c.steps if c else None,
                logical_ms=c.logical if c else None,
                wall_ms=round(wall[x.selected_attempt_id])
                if x.selected_attempt_id in wall
                else None,
            )
        )
    return out


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

    def get_experiment_evidence(self, experiment_id: UUID) -> ExperimentEvidence | None:
        with self._engine.connect() as conn:
            exp = conn.execute(
                select(t.experiments.c.config_json).where(t.experiments.c.id == experiment_id)
            ).first()
            if exp is None:
                return None
            return ExperimentEvidence(
                experiment_id=experiment_id,
                config=ExperimentCreate.model_validate(exp.config_json),
                slots=_evidence(conn, experiment_id),
            )
