"""Comparison report models (CONTRACTS.md section 4, ENGINEERING_PLAN.md section 6).

Canonical encoding forbids floats, so every rate is a ``Ratio``: exact integers plus a decimal
string rounded to four places. Every ratio carries its denominator; an empty denominator gives a
null value, never zero.
"""

from typing import Literal

from agentcrash.domain.models import Id, Sha256, Strict

Role = Literal["baseline", "candidate"]
REPORT_SCHEMA_VERSION = "1"


class Ratio(Strict):
    n: int
    d: int
    value: str | None  # n/d to four decimals; None when d is 0


class Summary(Strict):
    """Nearest-rank percentiles over ``count`` samples; ``warning`` marks small samples, for
    which no statistical-confidence claim is made."""

    count: int
    min: int | None
    p50: int | None
    p95: int | None
    max: int | None
    warning: Literal["SMALL_SAMPLE"] | None


class AgentCounts(Strict):
    submitted: int
    completed: int  # run reached `completed`
    errored: int  # run ended `error` (infrastructure or evaluator)
    cancelled: int
    passed: int
    failed: int
    inconclusive: int
    unfinished: int  # queued, running or evaluating
    pass_rate: Ratio  # passed over scheduled non-cancelled; inconclusive and errored are nonpasses
    conclusive_pass_rate: Ratio  # passed over passed + failed


class SafetyCounts(Strict):
    """Runs (not events) with each finding. ``attempted_unsafe`` includes committed ones."""

    attempted_unsafe: int
    committed_unsafe: int
    duplicate_write: int
    stale_source: int
    trace_incomplete: int


class Exposure(Strict):
    scenario_id: Id
    role: Role
    runs: int
    exposed_runs: int  # runs where at least one fault was applied
    applications: int
    exposure_rate: Ratio
    recovery_eligible: int  # exposed runs with a recovery grade that is not `unknown`
    recovered: int
    recovery_rate: Ratio  # recovered over recovery_eligible


class Delta(Strict):
    """Candidate minus baseline pass rate over complete pairs, as an exact signed ratio."""

    scenario_id: Id | None  # None: all scenarios
    pairs: int
    baseline_passes: int
    candidate_passes: int
    delta: Ratio  # n = candidate_passes - baseline_passes, d = pairs


class Side(Strict):
    status: str
    verdict: Literal["pass", "fail", "inconclusive"] | None
    primary_reason: str | None
    error_code: str | None


class Pair(Strict):
    scenario_id: Id
    scenario_version: int
    seed: str
    trial: int
    complete: bool
    baseline: Side | None
    candidate: Side | None


class MissingPair(Strict):
    scenario_id: Id
    scenario_version: int
    seed: str
    trial: int
    missing: list[Role]
    reasons: list[str]


class Reason(Strict):
    code: str
    detail: str


class Gate(Strict):
    status: Literal["pass", "fail", "incomplete"]
    reasons: list[Reason]


class UsageSummary(Strict):
    tool_calls: Summary
    steps: Summary
    # Mock runs make no model calls and have no price table: reported as unknown, not zero.
    model_requests: None = None
    tokens: None = None
    estimated_cost: None = None


class Latency(Strict):
    logical_ms: Summary
    wall_ms: Summary  # machine-dependent; informational only, never part of a verdict


class ComparisonReport(Strict):
    report_schema_version: Literal["1"]
    experiment_id: str
    manifest_hashes: list[Sha256]
    grader_set_hash: Sha256 | None
    gate_policy_id: Literal["mock-release.v1"]
    expected_pairs: int
    complete_pairs: int
    missing_pairs: list[MissingPair]
    counts_by_agent: dict[Role, AgentCounts]
    fault_exposure_by_scenario: list[Exposure]
    paired_completion_delta: list[Delta]  # overall first, then one per scenario
    safety_counts: dict[Role, SafetyCounts]
    latency_summary: dict[Role, Latency]
    usage_summary: dict[Role, UsageSummary]
    pairs: list[Pair]
    baseline_failures: list[Pair]  # designated baseline failures stay visible
    gate: Gate
