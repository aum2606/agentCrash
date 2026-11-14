import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TOKEN, apiError, json, mockApi, setup, signIn } from "./helpers";

setup();

const EXP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const RUN_B = "b0000000-0000-0000-0000-000000000001";
const RUN_C = "c0000000-0000-0000-0000-000000000002";
const ratio = (n: number, d: number) => ({ n, d, value: d === 0 ? null : (n / d).toFixed(4) });
const counts = (passed: number, failed: number) => ({
  submitted: passed + failed,
  completed: passed + failed,
  errored: 0,
  cancelled: 0,
  passed,
  failed,
  inconclusive: 0,
  unfinished: 0,
  pass_rate: ratio(passed, passed + failed),
  conclusive_pass_rate: ratio(passed, passed + failed),
});
const slot = (role: string, run_id: string, status = "completed", verdict: string | null = null) => ({
  scenario_id: "invoice-503",
  scenario_version: 1,
  seed: "7",
  trial: 0,
  role,
  run_id,
  status,
  verdict,
});
const progress = (done: number, total: number) => ({
  total,
  queued: total - done,
  running: 0,
  evaluating: 0,
  completed: done,
  error: 0,
});
const experiment = (status: string, slots: unknown[]) => ({
  id: EXP,
  name: "release check",
  gate_policy_id: "mock-release.v1",
  config_hash: "cfg",
  status,
  expected_runs: 2,
  progress: { baseline: progress(1, 1), candidate: progress(1, 1) },
  slots,
  created_at: "2026-11-14T10:00:00Z",
});
const summary = { count: 1, min: 5, p50: 5, p95: 5, max: 5, warning: "SMALL_SAMPLE" };
const safety = {
  attempted_unsafe: 0,
  committed_unsafe: 1,
  duplicate_write: 0,
  stale_source: 0,
  trace_incomplete: 0,
};
const usage = { tool_calls: summary, steps: summary, model_requests: null, tokens: null, estimated_cost: null };
const report = (gate: object, pairs: unknown[], missing: unknown[] = []) => ({
  report_schema_version: "1.0",
  experiment_id: EXP,
  manifest_hashes: [],
  grader_set_hash: "ab".repeat(32),
  gate_policy_id: "mock-release.v1",
  expected_pairs: 1,
  complete_pairs: missing.length ? 0 : 1,
  missing_pairs: missing,
  counts_by_agent: { baseline: counts(0, 1), candidate: counts(1, 0) },
  fault_exposure_by_scenario: [
    {
      scenario_id: "invoice-503",
      role: "baseline",
      runs: 1,
      exposed_runs: 1,
      applications: 2,
      exposure_rate: ratio(1, 1),
      recovery_eligible: 1,
      recovered: 0,
      recovery_rate: ratio(0, 1),
    },
  ],
  paired_completion_delta: [
    { scenario_id: null, pairs: 1, baseline_passes: 0, candidate_passes: 1, delta: ratio(1, 1) },
  ],
  safety_counts: { baseline: safety, candidate: { ...safety, committed_unsafe: 0 } },
  latency_summary: {
    baseline: { logical_ms: summary, wall_ms: summary },
    candidate: { logical_ms: summary, wall_ms: summary },
  },
  usage_summary: { baseline: usage, candidate: usage },
  pairs,
  baseline_failures: [],
  gate,
});
const side = (verdict: string | null, status: string, primary_reason: string | null = null) => ({
  verdict,
  status,
  primary_reason,
  error_code: null,
});
const PAIR = {
  scenario_id: "invoice-503",
  scenario_version: 1,
  seed: "7",
  trial: 0,
  complete: true,
  baseline: side("fail", "completed", "DUPLICATE_WRITE"),
  candidate: side("pass", "completed"),
};

describe("experiment page", () => {
  it("shows the gate, exact ratios, small-sample warning and links each cell to its run", async () => {
    const seen = mockApi({
      [`GET /api/v1/experiments/${EXP}`]: () =>
        json(
          experiment("completed", [
            slot("baseline", RUN_B, "completed", "fail"),
            slot("candidate", RUN_C, "completed", "pass"),
          ]),
        ),
      [`GET /api/v1/experiments/${EXP}/report`]: () =>
        json(report({ status: "pass", reasons: [{ code: "ALL_GOOD", detail: "candidate not worse" }] }, [PAIR])),
    });
    window.location.hash = `#/experiments/${EXP}`;
    signIn();
    await screen.findByText("pass", { selector: "strong" });
    expect(screen.getByText(/1 of 1 pairs complete/)).toBeTruthy();
    expect(screen.getByText("ALL_GOOD: candidate not worse")).toBeTruthy();
    expect(screen.getAllByText("1/1 (1.0000)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0/1 (0.0000)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("small sample: no confidence claim")).toHaveLength(4);
    const matrix = within(screen.getByRole("table", { name: /Paired results/ }));
    expect(matrix.getByRole("link", { name: "fail" }).getAttribute("href")).toBe(`#/runs/${RUN_B}`);
    expect(matrix.getByRole("link", { name: "pass" }).getAttribute("href")).toBe(`#/runs/${RUN_C}`);
    expect(screen.getByText(/DUPLICATE_WRITE/)).toBeTruthy();
    expect(seen.find((s) => s.path.endsWith("/report"))?.query.get("partial")).toBe("true");
  });

  it("says incomplete with missing pairs, shows empty ratios as such, and stops polling when done", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "running";
    const seen = mockApi({
      [`GET /api/v1/experiments/${EXP}`]: () => json(experiment(status, [slot("baseline", RUN_B, "running")])),
      [`GET /api/v1/experiments/${EXP}/report`]: () =>
        json({
          ...report(
            { status: "incomplete", reasons: [{ code: "MISSING_PAIRS", detail: "1 pair not finished" }] },
            [{ ...PAIR, complete: false, baseline: side(null, "running"), candidate: null }],
            [
              {
                scenario_id: "invoice-503",
                scenario_version: 1,
                seed: "7",
                trial: 0,
                missing: ["candidate"],
                reasons: ["NOT_FINISHED"],
              },
            ],
          ),
          counts_by_agent: { baseline: counts(0, 0), candidate: counts(0, 0) },
        }),
    });
    window.location.hash = `#/experiments/${EXP}`;
    signIn();
    await screen.findByText("incomplete", { selector: "strong" });
    expect(screen.getByText(/updating every second/)).toBeTruthy();
    expect(screen.getAllByText("0/0 (no samples)").length).toBeGreaterThan(0);
    expect(screen.getByText(/missing candidate \(NOT_FINISHED\)/)).toBeTruthy();

    status = "completed";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    await screen.findByText("completed", { selector: ".status" });
    const settled = seen.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(seen.length).toBe(settled);
  });

  it("shows an unknown experiment as a refusal and renders hostile text inertly", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    mockApi({
      [`GET /api/v1/experiments/${EXP}`]: () => apiError(404, "EXPERIMENT_NOT_FOUND", hostile),
      [`GET /api/v1/experiments/${EXP}/report`]: () => apiError(404, "EXPERIMENT_NOT_FOUND", hostile),
    });
    window.location.hash = `#/experiments/${EXP}`;
    signIn();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("EXPERIMENT_NOT_FOUND");
    expect(alert.textContent).toContain(hostile);
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("open an experiment", () => {
  it("only accepts a UUID, then navigates without putting the token in the URL", async () => {
    mockApi({
      "GET /api/v1/scenarios": () => json({ items: [], next_cursor: null }),
      "GET /api/v1/agents": () => json({ items: [] }),
      "GET /api/v1/runs": () => json({ items: [], next_cursor: null }),
    });
    window.location.hash = "";
    signIn();
    const input = await screen.findByLabelText("Experiment id");
    const open = screen.getByRole("button", { name: "Open experiment" }) as HTMLButtonElement;
    fireEvent.change(input, { target: { value: "not-an-id" } });
    expect(open.disabled).toBe(true);
    expect(screen.getByText(/not an experiment id/)).toBeTruthy();
    fireEvent.change(input, { target: { value: EXP } });
    expect(open.disabled).toBe(false);
    fireEvent.click(open);
    expect(window.location.hash).toBe(`#/experiments/${EXP}`);
    expect(window.location.href).not.toContain(TOKEN);
  });
});
