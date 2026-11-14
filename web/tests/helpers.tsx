import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { App } from "../src/App";
import type { Run } from "../src/api/client";

export const RUN_ID = "11111111-2222-3333-4444-555555555555";

export function makeRun(over: Partial<Run> = {}): Run {
  return {
    id: RUN_ID,
    status: "queued",
    verdict: null,
    manifest_hash: "sha256:abc",
    selected_attempt_id: null,
    attempts: [],
    parent_run_id: null,
    replay_capability: "none",
    scenario: { id: "invoice-healthy", version: 1 },
    agent: { id: "invoice-naive", version: "1.0.0" },
    seed: "42",
    labels: {},
    created_at: "2026-11-14T10:00:00Z",
    completed_at: null,
    error_code: null,
    ...over,
  };
}

export interface Seen {
  method: string;
  path: string;
  query: URLSearchParams;
  authorization: string | null;
  idempotency: string | null;
  body: unknown;
}

type Handler = (seen: Seen) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

export const apiError = (status: number, code: string, message: string) =>
  json(
    { error: { code, message, details: [], request_id: "r1", retryable: false } },
    status,
  );

/** Stub global fetch with `METHOD /path` handlers; returns every request seen. */
export function mockApi(routes: Record<string, Handler>) {
  const seen: Seen[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const url = new URL(input.url);
      const entry: Seen = {
        method: input.method,
        path: url.pathname,
        query: url.searchParams,
        authorization: input.headers.get("authorization"),
        idempotency: input.headers.get("idempotency-key"),
        body: input.method === "POST" ? await input.clone().json().catch(() => null) : null,
      };
      seen.push(entry);
      const handler = routes[`${entry.method} ${entry.path}`];
      if (!handler) return apiError(404, "NOT_FOUND", `no stub for ${entry.method} ${entry.path}`);
      return handler(entry);
    }),
  );
  return seen;
}

export function setup() {
  beforeEach(() => {
    window.location.hash = "";
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
}

export const TOKEN = "s3cret-token-value";

export function signIn(token = TOKEN) {
  render(<App />);
  enterToken(token);
}

export function enterToken(token = TOKEN) {
  fireEvent.change(screen.getByLabelText("Token"), { target: { value: token } });
  fireEvent.click(screen.getByRole("button", { name: "Use token" }));
}
