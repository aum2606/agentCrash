import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import {
  RUN_ID,
  TOKEN,
  apiError,
  json,
  makeRun,
  mockApi,
  enterToken,
  setup,
  signIn,
} from "./helpers";

setup();

const SCENARIOS = {
  items: [
    { id: "invoice-healthy", version: 1, content_hash: "h", description: "d", created_at: "t" },
    { id: "invoice-503", version: 1, content_hash: "h2", description: "d", created_at: "t" },
  ],
  next_cursor: null,
};
const AGENTS = {
  items: [
    { id: "invoice-naive", version: "1.0.0", config_schema: {}, source_hash: null },
    { id: "invoice-resilient", version: "1.0.0", config_schema: {}, source_hash: null },
  ],
};
const listing = (items: unknown[]) => ({ items, next_cursor: null });

const base = (runs: unknown[]) => ({
  "GET /api/v1/scenarios": () => json(SCENARIOS),
  "GET /api/v1/agents": () => json(AGENTS),
  "GET /api/v1/runs": () => json(listing(runs)),
});

describe("token handling", () => {
  it("asks for the token first and never stores it", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const seen = mockApi(base([]));
    render(<App />);
    expect(screen.getByLabelText("Token")).toHaveProperty("type", "password");
    expect(seen).toHaveLength(0);

    enterToken();
    await screen.findByText("No runs yet. Start one above.");

    expect(seen.every((s) => s.authorization === `Bearer ${TOKEN}`)).toBe(true);
    expect(setItem).not.toHaveBeenCalled();
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain(TOKEN);
    expect(document.cookie).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("is gone after a reload (a fresh app has no token)", () => {
    mockApi(base([]));
    signIn();
    cleanup();
    render(<App />);
    expect(screen.getByLabelText("Token")).toBeTruthy();
  });

  it("shows a rejected token and lets the user enter another", async () => {
    const refuse = () => apiError(401, "UNAUTHENTICATED", "A valid bearer token is required.");
    mockApi({
      "GET /api/v1/scenarios": refuse,
      "GET /api/v1/agents": refuse,
      "GET /api/v1/runs": refuse,
    });
    signIn("wrong");
    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]?.textContent).toContain("The token was rejected");
    fireEvent.click(screen.getAllByRole("button", { name: "Enter a different token" })[0]!);
    expect(screen.getByLabelText("Token")).toBeTruthy();
  });
});

describe("runs page", () => {
  it("lists runs with status as text and links to the detail page", async () => {
    mockApi(base([makeRun({ status: "completed", verdict: "fail" })]));
    signIn();
    const link = await screen.findByRole("link", { name: /invoice-healthy v1/ });
    expect(link.getAttribute("href")).toBe(`#/runs/${RUN_ID}`);
    const card = link.closest("li")!;
    expect(within(card).getByText("completed")).toBeTruthy();
    expect(card.textContent).toContain("verdict: fail");
  });

  it("submits explicit budgets, seed and an idempotency key, then opens the run", async () => {
    const seen = mockApi({
      ...base([]),
      "POST /api/v1/runs": () => json(makeRun(), 202),
    });
    signIn();
    await screen.findByRole("button", { name: "Start run" });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "invoice-resilient@1.0.0" } });
    fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Tool calls"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(window.location.hash).toBe(`#/runs/${RUN_ID}`));
    const post = seen.find((s) => s.method === "POST")!;
    expect(post.idempotency).toMatch(/^[0-9a-f-]{36}$/);
    expect(post.body).toEqual({
      schema_version: "1.0",
      mode: "mock",
      scenario: { id: "invoice-healthy", version: 1 },
      agent: { id: "invoice-resilient", version: "1.0.0", config: {} },
      seed: "7",
      budgets: { logical_ms: 60000, steps: 20, tool_calls: 10, wall_ms: 120000 },
      labels: {},
    });
  });

  it("shows why the server refused a submission", async () => {
    mockApi({
      ...base([]),
      "POST /api/v1/runs": () => apiError(429, "RATE_LIMITED", "Too many runs."),
    });
    signIn();
    fireEvent.click(await screen.findByRole("button", { name: "Start run" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("RATE_LIMITED: Too many runs.");
  });

  it("says so when nothing is registered, and when the API is down", async () => {
    mockApi({
      "GET /api/v1/scenarios": () => json(listing([])),
      "GET /api/v1/agents": () => json({ items: [] }),
      "GET /api/v1/runs": () => json(listing([])),
    });
    signIn();
    await screen.findByText(/No scenarios or agents are registered/);

    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("down"))));
    cleanup();
    signIn();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => a.textContent?.includes("The local API is not available"))).toBe(true);
  });
});

describe("run page", () => {
  it("polls every second while active, then slows down, and can cancel", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: "running" | "completed" = "running";
    const seen = mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(makeRun({ status })),
      [`POST /api/v1/runs/${RUN_ID}/cancel`]: () => json(makeRun({ status }), 202),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText(/updating every second/);
    const gets = () => seen.filter((s) => s.method === "GET").length;
    const before = gets();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(gets()).toBeGreaterThanOrEqual(before + 2);

    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(seen.some((s) => s.path.endsWith("/cancel"))).toBe(true));

    status = "completed";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    await screen.findByText("completed");
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
    const settled = gets();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(gets()).toBe(settled); // idle interval is 5 s
  });

  it("shows an error state and keeps the last good data", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let fail = false;
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () =>
        fail ? apiError(503, "NOT_READY", "database down") : json(makeRun({ status: "running" })),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText(/updating every second/);
    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("NOT_READY");
    expect(screen.getByText(/updating every second/)).toBeTruthy();
  });

  it("renders hostile text from the API inertly", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
    mockApi({
      [`GET /api/v1/runs/${RUN_ID}`]: () =>
        json(makeRun({ status: "error", error_code: hostile, completed_at: hostile })),
    });
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    await screen.findByText("Error code");
    expect(document.querySelector("img, script")).toBeNull();
    expect(document.body.textContent).toContain(hostile);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("shows loading, then a not-found problem for an unknown run", async () => {
    mockApi({});
    window.location.hash = `#/runs/${RUN_ID}`;
    signIn();
    expect(screen.getByText("Loading run…")).toBeTruthy();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("NOT_FOUND");
  });
});
