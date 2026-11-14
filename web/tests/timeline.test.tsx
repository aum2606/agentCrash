import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RUN_ID, json, makeRun, mockApi, setup, signIn } from "./helpers";

setup();

const ATTEMPT = "99999999-8888-7777-6666-555555555555";
const ev = (seq: number, type: string, payload: Record<string, unknown>, call_id: string | null = null) => ({
  run_id: RUN_ID, attempt_id: ATTEMPT, seq, type, wall_time: "2026-11-14T10:00:0" + seq + "Z",
  logical_time_ms: seq * 100, actor_id: "agent", call_id, payload, prev_hash: "p", event_hash: "h",
});
const EVENTS = [
  ev(1, "tool.requested", { tool: "invoice.get" }, "c1"),
  ev(2, "fault.decided", { effect: "schema_mutation", tool: "invoice.get", rule_id: "r1", applied: true }, "c1"),
  ev(3, "tool.completed", {
    tool: "invoice.get", status: 200, body_ref: "sha256:MUTATED", original_body_ref: "sha256:ORIGINAL",
  }, "c1"),
];
const EVALUATION = {
  id: "e1", attempt_id: ATTEMPT, grader_set_hash: "g", snapshot_hash: null, verdict: "fail",
  primary_reason: "STALE_READ", findings: [{ code: "STALE_READ", source: "g1", detail: null }],
  evaluated_at: "2026-11-14T10:01:00Z",
  grades: [{
    grader_id: "correct_post", grader_version: 1, grader_hash: "x", required: true, result: "fail",
    reason_code: "WRONG_AMOUNT", expected: 10, observed: 12,
    evidence: [{ kind: "event", ref: "3" }, { kind: "state", ref: "invoice-1", path: "/total" }],
  }],
  duration_ms: 0,
};
const run = makeRun({
  status: "completed", verdict: "fail", selected_attempt_id: ATTEMPT,
  attempts: [{ id: ATTEMPT, ordinal: 1, termination: "completed" }],
});

describe("run timeline", () => {
  it("shows fault badges, original vs mutated hashes, and sends exact query", async () => {
    const seen = mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(run),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () =>
        json({ items: EVENTS, next_after_seq: 3, has_more: false, attempt_closed: true }),
      [`GET /api/v1/runs/${RUN_ID}/evaluations`]: () => json({ items: [EVALUATION] }),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText("fault injected");
    expect(screen.getByText("sha256:ORIGINAL")).toBeTruthy();
    expect(screen.getByText("sha256:MUTATED")).toBeTruthy();
    expect(screen.getByText("Original body (before fault)")).toBeTruthy();
    const q = seen.find((s) => s.path.endsWith("/events"));
    expect(q?.query.get("attempt_id")).toBe(ATTEMPT);
    expect(q?.query.get("limit")).toBe("200");
    expect(screen.getByText(/primary reason STALE_READ/)).toBeTruthy();
  });

  it("links a grade to the event it cites and lists non-event evidence as text", async () => {
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(run),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () =>
        json({ items: EVENTS, next_after_seq: 3, has_more: false, attempt_closed: true }),
      [`GET /api/v1/runs/${RUN_ID}/evaluations`]: () => json({ items: [EVALUATION] }),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    const button = await screen.findByRole("button", { name: "Show event #3" });
    expect(screen.queryByText("cited by a grade")).toBeNull();
    fireEvent.click(button);
    const row = document.getElementById("event-3") as HTMLElement;
    expect(within(row).getByText("cited by a grade")).toBeTruthy();
    expect(screen.getByText(/state invoice-1 \/total/)).toBeTruthy();
  });

  it("follows pagination and says when nothing is evaluated or recorded", async () => {
    const pages: Record<string, unknown> = {
      "0": { items: [EVENTS[0]], next_after_seq: 1, has_more: true, attempt_closed: false },
      "1": { items: [EVENTS[2]], next_after_seq: 3, has_more: false, attempt_closed: false },
    };
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(makeRun({ ...run, status: "completed" })),
      [`GET /api/v1/runs/${RUN_ID}/events`]: (s) =>
        json(pages[s.query.get("after_seq") ?? "0"]),
      [`GET /api/v1/runs/${RUN_ID}/evaluations`]: () => json({ items: [] }),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText("Not evaluated yet.");
    expect(screen.getByText(/still open/)).toBeTruthy();
    expect(screen.getByText("#1")).toBeTruthy(); // both pages are shown
    expect(screen.getByText("#3")).toBeTruthy();
  });

  it("stops with an error when the events cursor does not advance", async () => {
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(run),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () =>
        json({ items: [EVENTS[0]], next_after_seq: 0, has_more: true, attempt_closed: false }),
      [`GET /api/v1/runs/${RUN_ID}/evaluations`]: () => json({ items: [] }),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    expect((await screen.findByRole("alert")).textContent).toContain("BAD_CURSOR");
  });

  it("renders hostile payload text inertly", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(run),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () =>
        json({ items: [ev(1, "tool.completed", { tool: hostile, status: 200 })], next_after_seq: 1, has_more: false, attempt_closed: true }),
      [`GET /api/v1/runs/${RUN_ID}/evaluations`]: () => json({ items: [] }),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText(`${hostile} → 200`);
    expect(document.querySelector("img")).toBeNull();
  });
});
