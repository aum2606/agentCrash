"""Experiment matrix: the immutable set of `(scenario, seed, trial)` cells, two runs per cell
(one per agent role), and the checks that make a baseline/candidate pair comparable."""

from dataclasses import dataclass

from agentcrash.domain.models import ExperimentCreate, Manifest
from agentcrash.domain.report import Role

ROLES: tuple[Role, ...] = ("baseline", "candidate")


@dataclass(frozen=True, order=True)
class Cell:
    scenario_id: str
    scenario_version: int
    seed: str
    trial: int


@dataclass(frozen=True)
class Slot:
    cell: Cell
    role: Role


def expand(create: ExperimentCreate) -> list[Slot]:
    """Deterministic order: scenarios as listed, then seeds as listed, then trial, baseline
    before candidate. The same request always yields the same slots."""
    return [
        Slot(Cell(ref.id, ref.version, seed, trial), role)
        for ref in create.scenario_refs
        for seed in create.seeds
        for trial in range(1, create.trials_per_seed + 1)
        for role in ROLES
    ]


def cells(create: ExperimentCreate) -> list[Cell]:
    return [s.cell for s in expand(create) if s.role == "baseline"]


def incompatibilities(baseline: Manifest, candidate: Manifest) -> list[str]:
    """Stable codes for everything besides the agent that must match for a pair to be a fair
    comparison. Empty means comparable. Agents differ by design and are not compared."""
    checks = {
        "SCENARIO_MISMATCH": baseline.scenario_hash == candidate.scenario_hash,
        "FIXTURE_MISMATCH": baseline.fixture == candidate.fixture,
        "GRADER_SET_MISMATCH": (
            baseline.grader_set_hash == candidate.grader_set_hash
            and baseline.graders == candidate.graders
        ),
        "SEED_MISMATCH": baseline.seed == candidate.seed,
        "MODE_MISMATCH": baseline.mode == candidate.mode,
        "EPOCH_MISMATCH": baseline.logical_epoch == candidate.logical_epoch,
        "BUDGET_MISMATCH": baseline.budgets == candidate.budgets,
        "CANONICALIZATION_MISMATCH": (
            baseline.canonicalization_version == candidate.canonicalization_version
        ),
        "FAULT_ENGINE_MISMATCH": baseline.fault_engine_version == candidate.fault_engine_version,
        "TAXONOMY_MISMATCH": baseline.taxonomy_version == candidate.taxonomy_version,
        "REDACTION_MISMATCH": baseline.redaction == candidate.redaction,
    }
    return [code for code, same in checks.items() if not same]
