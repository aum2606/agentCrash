import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RUN_ID, apiError, json, makeRun, mockApi, setup, signIn } from "./helpers";

setup();

const ATTEMPT = "99999999-8888-7777-6666-555555555555";
const NEW_RUN = "dddddddd-2222-3333-4444-555555555555";
const JOB = "eeeeeeee-2222-3333-4444-555555555555";
const HASH = "ab".repeat(32);
const closed = makeRun({
  status: "completed",
  verdict: "pass",
  replay_capability: "full",
  selected_attempt_id: ATTEMPT,
  attempts: [{ id: ATTEMPT, ordinal: 1, termination: "completed" }],
});
const empty = {
  [`GET /api/v1/runs/${RUN_ID}/events`]: () =>
    json({ items: [], next_after_seq: 0, has_more: false, attempt_closed: true }),
  ["GET /api/v1/runs/" + RUN_ID + "/evaluations"]: () => json({ items: [] }),
};
const base = { [`GET /api/v1/runs/${RUN_ID}`]: () => json(closed), ...empty };
const open = () => {
  window.location.hash = `#/runs/${RUN_ID}`;
  signIn();
};
const replay = (over: object) => ({
  id: JOB,
  run_id: RUN_ID,
  attempt_id: ATTEMPT,
  mode: "strict",
  status: "completed",
  divergence: null,
  error_code: null,
  adapter_hash: null,
  consumed_events: 12,
  total_events: 12,
  created_at: "t",
  closed_at: "t",
  ...over,
});

describe("run actions", () => {
  it("offers no actions while the run is active or the attempt is still open", async () => {
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () =>
        json(makeRun({ status: "running", selected_attempt_id: ATTEMPT, attempts: [{ id: ATTEMPT, ordinal: 1, termination: null }] })),
      ...empty,
    });
    open();
    await screen.findByText(/updating every second/);
    expect(screen.queryByText("Actions")).toBeNull();
  });

  it("reruns with the exact body and a fresh key, then opens the new run", async () => {
    const seen = mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/reruns`]: () => json(makeRun({ id: NEW_RUN, parent_run_id: RUN_ID }), 202),
      [`GET /api/v1/runs/${NEW_RUN}`]: () => json(makeRun({ id: NEW_RUN })),
    });
    open();
    fireEvent.change(await screen.findByLabelText(/Seed \(optional/), { target: { value: " 77 " } });
    fireEvent.click(screen.getByRole("button", { name: "Start rerun" }));
    await waitFor(() => expect(window.location.hash).toBe(`#/runs/${NEW_RUN}`));
    const post = seen.find((s) => s.method === "POST");
    expect(post?.body).toEqual({ seed: "77" });
    expect(post?.idempotency).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("reruns with an empty body when no seed is given", async () => {
    const seen = mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/reruns`]: () => json(makeRun({ id: NEW_RUN }), 202),
      [`GET /api/v1/runs/${NEW_RUN}`]: () => json(makeRun({ id: NEW_RUN })),
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Start rerun" }));
    await waitFor(() => expect(seen.some((s) => s.method === "POST")).toBe(true));
    expect(seen.find((s) => s.method === "POST")?.body).toEqual({});
  });

  it("starts a strict replay for the attempt and reports an exact match", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "running";
    const seen = mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/replays`]: () => json(replay({ status: "queued", consumed_events: null, total_events: null }), 202),
      [`GET /api/v1/replays/${JOB}`]: () => json(replay({ status })),
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Start strict replay" }));
    await screen.findByText(/Strict replay/);
    expect(seen.find((s) => s.method === "POST")?.body).toEqual({ mode: "strict", attempt_id: ATTEMPT });
    status = "completed";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    await screen.findByText(/matched the recording exactly/);
    expect(screen.getByText(/12 of 12 recorded events/)).toBeTruthy();
    const polls = () => seen.filter((s) => s.path === `/api/v1/replays/${JOB}`).length;
    const settled = polls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(polls()).toBe(settled); // a finished job is not polled again
  });

  it("shows the first divergence as text, not as a pass", async () => {
    mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/replays`]: () => json(replay({}), 202),
      [`GET /api/v1/replays/${JOB}`]: () => json(replay({ divergence: { seq: 4, expected: "<b>x</b>" } })),
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Start strict replay" }));
    await screen.findByText(/diverged from the recording/);
    expect(screen.queryByText(/matched the recording exactly/)).toBeNull();
    expect(document.querySelector("pre")?.textContent).toContain("<b>x</b>");
    expect(document.querySelector("b")).toBeNull();
  });

  it("shows why a replay is refused", async () => {
    mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/replays`]: () =>
        json(
          {
            error: {
              code: "REPLAY_INCOMPATIBLE",
              message: "Strict replay is not possible for this run.",
              details: [{ path: "/", code: "CASSETTE_ABSENT" }],
              request_id: "r1",
              retryable: false,
            },
          },
          409,
        ),
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Start strict replay" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("REPLAY_INCOMPATIBLE");
    expect(alert.textContent).toContain("CASSETTE_ABSENT");
  });

  it("exports with the pin choice and shows the bundle hash without offering a download", async () => {
    const seen = mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/exports`]: () =>
        json({ id: JOB, run_id: RUN_ID, attempt_id: ATTEMPT, status: "completed", pin: false, artifact_hash: HASH, error_code: null, created_at: "t", closed_at: "t" }, 202),
      [`GET /api/v1/exports/${JOB}`]: () =>
        json({ id: JOB, run_id: RUN_ID, attempt_id: ATTEMPT, status: "completed", pin: false, artifact_hash: HASH, error_code: null, created_at: "t", closed_at: "t" }),
    });
    open();
    fireEvent.click(await screen.findByLabelText("Pin the bundle"));
    fireEvent.click(screen.getByRole("button", { name: "Export bundle" }));
    await screen.findByText(`${HASH}.acbundle`);
    expect(seen.find((s) => s.method === "POST")?.body).toEqual({ attempt_id: ATTEMPT, pin: false });
    expect(screen.getByText(/cannot download it/)).toBeTruthy();
    expect(document.querySelector("a[download]")).toBeNull();
  });

  it("shows a refused export (attempt not closed)", async () => {
    mockApi({
      ...base,
      [`POST /api/v1/runs/${RUN_ID}/exports`]: () => apiError(409, "ATTEMPT_NOT_CLOSED", "still active"),
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Export bundle" }));
    expect((await screen.findByRole("alert")).textContent).toContain("ATTEMPT_NOT_CLOSED");
  });
});
