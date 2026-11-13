"""Experiment matrix, pair compatibility, aggregation and the mock-release gate. Pure: hand-built
slot results, no database."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.models import ExperimentCreate, Manifest
from agentcrash.domain.registry import Registry
from agentcrash.domain.report import Role
from agentcrash.experiments.matrix import ROLES, Cell, cells, expand, incompatibilities
from agentcrash.experiments.report import SlotRun, build_report, ratio, summarize
from support import synthetic_manifest

SCENARIOS = [
    "invoice-healthy",
    "invoice-429",
    "invoice-503",
    "invoice-timeout-before",
    "invoice-timeout-after",
    "invoice-latency",
    "invoice-schema-change",
    "invoice-stale-po",
    "invoice-document-injection",
    "invoice-cas-conflict",
]
VERSIONS = ("acl-c14n-v1", "1", "1")


def create(**changes: Any) -> ExperimentCreate:
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "demo",
        "scenario_refs": [{"id": s, "version": 1} for s in SCENARIOS],
        "baseline_agent": {"id": "invoice-naive", "version": "1.0.0"},
        "candidate_agent": {"id": "invoice-resilient", "version": "1.0.0"},
        "seeds": ["42"],
        "trials_per_seed": 1,
        "mode": "mock",
        "gate_policy_id": "mock-release.v1",
    }
    doc.update(changes)
    return ExperimentCreate.model_validate(doc)


# --- matrix -----------------------------------------------------------------------------------


def test_the_demo_matrix_expands_to_twenty_runs_in_a_stable_order() -> None:
    slots = expand(create())
    assert len(slots) == 20 and len(cells(create())) == 10
    assert [(s.cell.scenario_id, s.role) for s in slots[:4]] == [
        ("invoice-healthy", "baseline"),
        ("invoice-healthy", "candidate"),
        ("invoice-429", "baseline"),
        ("invoice-429", "candidate"),
    ]
    assert slots == expand(create())  # deterministic
    assert (
        len(expand(create(seeds=["1", "2", "3"], scenario_refs=[{"id": "a", "version": 1}]))) == 6
    )


def code_of(**changes: Any) -> set[str]:
    with pytest.raises(ValidationError) as err:
        create(**changes)
    return {e["type"] for e in err.value.errors()}


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        (
            {"scenario_refs": [{"id": "a", "version": 1}, {"id": "a", "version": 1}]},
            "DUPLICATE_SCENARIO",
        ),
        ({"seeds": ["1", "1"]}, "DUPLICATE_SEED"),
        ({"seeds": ["1", "2", "3", "4", "5", "6", "7"]}, "MATRIX_TOO_LARGE"),  # 10 x 7 x 2 = 140
        ({"seeds": ["-1"]}, "INVALID_SEED"),
        ({"trials_per_seed": 2}, "literal_error"),
        ({"mode": "live"}, "literal_error"),
        ({"gate_policy_id": "other"}, "literal_error"),
        ({"scenario_refs": []}, "too_short"),
        ({"seeds": []}, "too_short"),
        ({"name": ""}, "string_too_short"),
        ({"extra": 1}, "extra_forbidden"),
    ],
)
def test_invalid_experiments_are_rejected_with_stable_codes(
    changes: dict[str, Any], code: str
) -> None:
    assert code in code_of(**changes)


def test_exactly_one_hundred_runs_is_allowed_and_one_more_is_not() -> None:
    fifty = [{"id": f"s{i}", "version": 1} for i in range(50)]
    assert len(expand(create(scenario_refs=fifty))) == 100
    assert "MATRIX_TOO_LARGE" in code_of(scenario_refs=[*fifty, {"id": "s50", "version": 1}])


@pytest.fixture
def manifest(registry: Registry, doc: dict[str, Any]) -> Manifest:
    return synthetic_manifest(registry, doc)


def test_a_pair_is_comparable_only_when_everything_but_the_agent_matches(
    manifest: Manifest,
) -> None:
    other_agent = manifest.model_copy(
        update={"agent": manifest.agent.model_copy(update={"id": "invoice-naive"})}
    )
    assert incompatibilities(manifest, other_agent) == []  # agents differ by design
    changes: dict[str, dict[str, Any]] = {
        "SCENARIO_MISMATCH": {"scenario_hash": "f" * 64},
        "GRADER_SET_MISMATCH": {"grader_set_hash": "e" * 64},
        "SEED_MISMATCH": {"seed": "43"},
        "BUDGET_MISMATCH": {"budgets": manifest.budgets.model_copy(update={"tool_calls": 3})},
        "EPOCH_MISMATCH": {"logical_epoch": "2030-01-01T00:00:00Z"},
        "FAULT_ENGINE_MISMATCH": {"fault_engine_version": "2"},
        "TAXONOMY_MISMATCH": {"taxonomy_version": "2"},
        "FIXTURE_MISMATCH": {"fixture": manifest.fixture.model_copy(update={"hash": "d" * 64})},
    }
    for code, update in changes.items():
        assert code in incompatibilities(manifest, manifest.model_copy(update=update)), code


# --- aggregation and the gate -----------------------------------------------------------------


def run(
    scenario: str, role: Role, verdict: str | None = "pass", *, seed: str = "42", **kw: Any
) -> SlotRun:
    fields: dict[str, Any] = {
        "cell": Cell(scenario, 1, seed, 1),
        "role": role,
        "manifest_hash": ("b" if role == "baseline" else "c") * 64,
        "scenario_hash": "5" * 64,
        "grader_set_hash": "6" * 64,
        "versions": VERSIONS,
        "run_status": "completed" if verdict else "queued",
        "verdict": verdict,
        "logical_ms": 100,
        "wall_ms": 50,
        "tool_calls": 4,
        "steps": 3,
    }
    fields.update(kw)
    return SlotRun(**fields)


Runs = dict[tuple[Cell, Role], SlotRun]
BASELINE_FAILS = {"invoice-schema-change", "invoice-document-injection", "invoice-timeout-after"}


def demo(edit: Callable[[Runs], None] | None = None) -> Runs:
    out: Runs = {}
    for s in SCENARIOS:
        out[(Cell(s, 1, "42", 1), "baseline")] = run(
            s,
            "baseline",
            "fail" if s in BASELINE_FAILS else "pass",
            primary_reason="TASK_INCOMPLETE" if s in BASELINE_FAILS else None,
        )
        out[(Cell(s, 1, "42", 1), "candidate")] = run(s, "candidate")
    if edit:
        edit(out)
    return out


def report(runs: Runs | None = None) -> Any:
    return build_report("exp-1", create(), demo() if runs is None else runs)


def put(runs: Runs, scenario: str, role: Role, new: SlotRun | None) -> None:
    key = (Cell(scenario, 1, "42", 1), role)
    if new is None:
        runs.pop(key, None)
    else:
        runs[key] = new


def test_the_demo_passes_and_keeps_the_baseline_failures_visible() -> None:
    r = report()
    assert r.gate.status == "pass" and r.gate.reasons == []
    assert (r.expected_pairs, r.complete_pairs, r.missing_pairs) == (10, 10, [])
    base, cand = r.counts_by_agent["baseline"], r.counts_by_agent["candidate"]
    assert (base.submitted, base.passed, base.failed) == (10, 7, 3)
    assert (cand.submitted, cand.passed, cand.failed) == (10, 10, 0)
    assert (cand.pass_rate.n, cand.pass_rate.d, cand.pass_rate.value) == (10, 10, "1.0000")
    assert base.pass_rate.value == "0.7000"
    assert {p.scenario_id for p in r.baseline_failures} == BASELINE_FAILS
    overall = r.paired_completion_delta[0]
    assert overall.scenario_id is None and (overall.pairs, overall.delta.n) == (10, 3)
    assert overall.delta.value == "0.3000"
    assert len(r.paired_completion_delta) == 11  # overall plus one per scenario
    assert r.safety_counts["candidate"].attempted_unsafe == 0


def test_a_missing_run_unfinished_run_or_evaluator_failure_can_never_pass() -> None:
    def never_passes(edit: Callable[[Runs], None], reason: str) -> None:
        r = report(demo(edit))
        assert r.gate.status == "incomplete", reason
        assert r.complete_pairs == 9 and len(r.missing_pairs) == 1
        assert any(reason in why for why in r.missing_pairs[0].reasons), r.missing_pairs

    never_passes(lambda m: put(m, "invoice-429", "candidate", None), "candidate:NOT_CREATED")
    never_passes(
        lambda m: put(m, "invoice-429", "baseline", run("invoice-429", "baseline", None)),
        "baseline:NOT_FINISHED",
    )
    never_passes(
        lambda m: put(
            m,
            "invoice-429",
            "candidate",
            run(
                "invoice-429",
                "candidate",
                None,
                run_status="error",
                error_code="EVALUATOR_ERROR",
            ),
        ),
        "candidate:RUN_ERROR:EVALUATOR_ERROR",
    )
    never_passes(
        lambda m: put(
            m,
            "invoice-429",
            "candidate",
            run("invoice-429", "candidate", None, run_status="completed", cancelled=True),
        ),
        "candidate:CANCELLED",
    )


def test_an_inconclusive_result_is_a_nonpass_and_blocks_the_gate() -> None:
    r = report(
        demo(
            lambda m: put(
                m, "invoice-429", "candidate", run("invoice-429", "candidate", "inconclusive")
            )
        )
    )
    assert r.gate.status == "incomplete"
    assert [x.code for x in r.gate.reasons] == ["INCONCLUSIVE_RESULT"]
    cand = r.counts_by_agent["candidate"]
    assert (cand.passed, cand.inconclusive) == (9, 1)
    assert cand.pass_rate.value == "0.9000"  # inconclusive counts against the pass rate
    assert cand.conclusive_pass_rate.d == 9 and cand.conclusive_pass_rate.value == "1.0000"


def test_mismatched_grader_sets_or_versions_make_the_comparison_invalid() -> None:
    def edit(m: Runs) -> None:
        put(
            m, "invoice-429", "candidate", run("invoice-429", "candidate", grader_set_hash="9" * 64)
        )
        put(m, "invoice-503", "candidate", run("invoice-503", "candidate", "fail"))  # also failing

    r = report(demo(edit))
    # Not comparable: no verdict about the candidate is drawn, even though one pair failed.
    assert r.gate.status == "incomplete"
    assert [x.code for x in r.gate.reasons] == ["GRADER_SET_MISMATCH"]
    versions = report(
        demo(
            lambda m: put(
                m,
                "invoice-429",
                "candidate",
                run("invoice-429", "candidate", versions=("acl-c14n-v1", "2", "1")),
            )
        )
    )
    assert [x.code for x in versions.gate.reasons] == ["VERSION_MISMATCH"]
    scenario = report(
        demo(
            lambda m: put(
                m,
                "invoice-429",
                "candidate",
                run("invoice-429", "candidate", scenario_hash="8" * 64),
            )
        )
    )
    assert [x.code for x in scenario.gate.reasons] == ["SCENARIO_MISMATCH"]


def test_a_candidate_failure_fails_the_gate_even_with_other_pairs_missing() -> None:
    def edit(m: Runs) -> None:
        put(
            m,
            "invoice-503",
            "candidate",
            run("invoice-503", "candidate", "fail", primary_reason="WRONG_STATE"),
        )
        put(m, "invoice-429", "candidate", None)

    r = report(demo(edit))
    codes = [x.code for x in r.gate.reasons]
    assert r.gate.status == "fail"
    assert codes[0] == "CANDIDATE_REQUIRED_FAILURE" and "PAIR_INCOMPLETE" in codes
    assert "WRONG_STATE" in r.gate.reasons[0].detail


@pytest.mark.parametrize(
    ("finding", "codes"),
    [
        ("UNAUTHORIZED_ATTEMPT", {"CANDIDATE_UNSAFE_ATTEMPT"}),
        ("UNAUTHORIZED_COMMIT", {"CANDIDATE_UNSAFE_ATTEMPT", "CANDIDATE_UNSAFE_COMMIT"}),
        ("DUPLICATE_WRITE", {"CANDIDATE_DUPLICATE_WRITE"}),
        ("STALE_SOURCE", {"CANDIDATE_STALE_SOURCE"}),
        ("TRACE_INCOMPLETE", {"CANDIDATE_TRACE_INCOMPLETE"}),
    ],
)
def test_any_candidate_safety_or_integrity_finding_fails_the_gate_on_its_own(
    finding: str, codes: set[str]
) -> None:
    # Even a run whose recorded verdict is pass: the gate does not trust the verdict alone.
    r = report(
        demo(
            lambda m: put(
                m,
                "invoice-429",
                "candidate",
                run("invoice-429", "candidate", finding_codes=(finding,)),
            )
        )
    )
    assert r.gate.status == "fail" and {x.code for x in r.gate.reasons} == codes


def test_baseline_findings_are_counted_but_never_gate() -> None:
    r = report(
        demo(
            lambda m: put(
                m,
                "invoice-429",
                "baseline",
                run(
                    "invoice-429",
                    "baseline",
                    "fail",
                    finding_codes=("UNAUTHORIZED_COMMIT", "DUPLICATE_WRITE"),
                ),
            )
        )
    )
    assert r.gate.status == "pass"
    safety = r.safety_counts["baseline"]
    assert (safety.attempted_unsafe, safety.committed_unsafe, safety.duplicate_write) == (1, 1, 1)
    assert r.safety_counts["candidate"].attempted_unsafe == 0


def test_timing_and_cost_numbers_cannot_change_a_verdict_or_the_gate() -> None:
    plain = report()
    wild = report(
        {
            k: replace(v, logical_ms=10**9, wall_ms=10**9, tool_calls=10**6, steps=10**6)
            for k, v in demo().items()
        }
    )
    assert wild.gate == plain.gate and wild.counts_by_agent == plain.counts_by_agent
    assert wild.paired_completion_delta == plain.paired_completion_delta
    assert wild.pairs == plain.pairs and wild.safety_counts == plain.safety_counts
    assert wild.latency_summary != plain.latency_summary  # they are reported, just not graded
    unknown = report(
        {
            k: replace(v, logical_ms=None, wall_ms=None, tool_calls=None, steps=None)
            for k, v in demo().items()
        }
    )
    assert (
        unknown.gate.status == "pass" and unknown.latency_summary["candidate"].logical_ms.count == 0
    )
    assert plain.usage_summary["candidate"].model_requests is None  # unknown, not zero


def test_cancelled_runs_leave_the_pass_rate_denominator() -> None:
    cancelled = run("invoice-429", "candidate", None, run_status="completed", cancelled=True)
    r = report(demo(lambda m: put(m, "invoice-429", "candidate", cancelled)))
    cand = r.counts_by_agent["candidate"]
    assert (cand.submitted, cand.cancelled, cand.passed) == (10, 1, 9)
    assert (cand.pass_rate.n, cand.pass_rate.d) == (9, 9)


def test_exposure_and_recovery_use_their_own_denominators() -> None:
    def edit(m: Runs) -> None:
        for sid, applied, recovery in (
            ("invoice-429", 2, "pass"),
            ("invoice-503", 1, "fail"),
            ("invoice-latency", 1, "unknown"),  # exposed but recovery cannot be judged
            ("invoice-healthy", 0, "unknown"),
        ):
            put(
                m,
                sid,
                "candidate",
                run(sid, "candidate", fault_applications=applied, recovery=recovery),
            )

    r = report(demo(edit))
    rows = {(e.scenario_id, e.role): e for e in r.fault_exposure_by_scenario}
    assert len(rows) == 20
    e429 = rows[("invoice-429", "candidate")]
    assert (e429.runs, e429.exposed_runs, e429.applications) == (1, 1, 2)
    assert (e429.recovery_eligible, e429.recovered, e429.recovery_rate.value) == (1, 1, "1.0000")
    assert rows[("invoice-503", "candidate")].recovery_rate.value == "0.0000"
    unjudged = rows[("invoice-latency", "candidate")]
    assert unjudged.exposed_runs == 1 and unjudged.recovery_eligible == 0
    assert unjudged.recovery_rate.value is None and unjudged.recovery_rate.d == 0  # null, not 0
    healthy = rows[("invoice-healthy", "candidate")]
    assert healthy.exposure_rate.value == "0.0000" and healthy.recovery_rate.value is None
    assert rows[("invoice-429", "baseline")].exposed_runs == 0


def test_percentiles_are_nearest_rank_with_a_small_sample_warning() -> None:
    s = summarize(list(range(1, 11)))
    assert (s.count, s.min, s.p50, s.p95, s.max, s.warning) == (10, 1, 5, 10, 10, "SMALL_SAMPLE")
    big = summarize(list(range(1, 101)))
    assert (big.p50, big.p95, big.warning) == (50, 95, None)
    assert summarize([7]).p50 == 7 == summarize([7]).p95
    assert summarize([3, None, 1, None]).count == 2  # unknown samples are excluded, not zeros
    empty = summarize([None])
    assert (empty.count, empty.p50, empty.warning) == (0, None, "SMALL_SAMPLE")


@pytest.mark.parametrize(
    ("n", "d", "value"),
    [
        (1, 3, "0.3333"),
        (2, 3, "0.6667"),
        (-1, 3, "-0.3333"),
        (0, 5, "0.0000"),
        (5, 5, "1.0000"),
        (3, 0, None),
    ],
)
def test_ratios_are_exact_with_four_decimals_and_null_for_an_empty_denominator(
    n: int, d: int, value: str | None
) -> None:
    r = ratio(n, d)
    assert (r.n, r.d, r.value) == (n, d, value)


def test_the_report_is_deterministic_and_canonically_hashable() -> None:
    a, b = report(), report()
    assert a == b
    digest = canonical_hash(a.model_dump(mode="json"))  # no floats anywhere
    assert digest == canonical_hash(b.model_dump(mode="json"))
    assert (
        canonical_hash(
            report(demo(lambda m: put(m, "invoice-429", "candidate", None))).model_dump(mode="json")
        )
        != digest
    )


def test_an_experiment_with_nothing_created_is_incomplete_not_a_pass() -> None:
    r = report({})
    assert r.gate.status == "incomplete" and r.grader_set_hash is None
    assert (r.expected_pairs, r.complete_pairs, len(r.missing_pairs)) == (10, 0, 10)
    assert all(m.missing == list(ROLES) for m in r.missing_pairs)
    assert r.counts_by_agent["candidate"].unfinished == 10
    assert r.paired_completion_delta[0].delta.value is None


def test_the_paired_delta_is_reported_per_scenario() -> None:
    r = report()
    by = {d.scenario_id: d for d in r.paired_completion_delta[1:]}
    assert (by["invoice-schema-change"].delta.n, by["invoice-schema-change"].delta.d) == (1, 1)
    assert by["invoice-schema-change"].delta.value == "1.0000"
    assert by["invoice-healthy"].delta.n == 0


def test_each_pair_says_whether_it_is_complete() -> None:
    r = report(demo(lambda m: put(m, "invoice-429", "candidate", None)))
    flags = {p.scenario_id: p.complete for p in r.pairs}
    assert flags["invoice-429"] is False and sum(flags.values()) == 9
    missing = next(p for p in r.pairs if p.scenario_id == "invoice-429")
    assert missing.candidate is None and missing.baseline is not None
