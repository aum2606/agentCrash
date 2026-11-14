import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Run = components["schemas"]["RunRecord"];
export type RunCreate = components["schemas"]["RunCreate"];
export type ScenarioSummary = components["schemas"]["ScenarioSummary"];
export type AgentInfo = components["schemas"]["AgentInfo"];
export type EventRecord = components["schemas"]["EventRecord"];
export type Evaluation = components["schemas"]["EvaluationRecord"];
export type Budgets = components["schemas"]["Budgets"];

/** Everything the UI can fail on, in one shape: `kind` tells infrastructure from API errors. */
export class ApiProblem extends Error {
  constructor(
    readonly kind: "api" | "auth" | "network",
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, string>[] = [],
  ) {
    super(message);
  }
}

/** Same-origin client. The token is read per request from memory, never stored by this module. */
export function createApi(
  getToken: () => string | null,
  options: { baseUrl?: string; fetch?: typeof fetch } = {},
) {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? window.location.origin,
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  client.use({
    onRequest({ request }) {
      const token = getToken();
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
  });
  return client;
}
export type Api = ReturnType<typeof createApi>;

interface Settled<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Unwrap an openapi-fetch result: data, or a thrown ApiProblem. */
export async function call<T>(operation: Promise<Settled<T>>): Promise<T> {
  let settled: Settled<T>;
  try {
    settled = await operation;
  } catch {
    throw new ApiProblem("network", 0, "UNREACHABLE", "Cannot reach the local API.");
  }
  const { data, error, response } = settled;
  if (response.ok && data !== undefined) return data;
  const body = (error as { error?: Partial<components["schemas"]["ErrorBody"]> } | undefined)
    ?.error;
  const code = body?.code ?? `HTTP_${response.status}`;
  const message = body?.message ?? "The API returned an unexpected response.";
  const kind = response.status === 401 ? "auth" : response.status >= 500 ? "network" : "api";
  throw new ApiProblem(kind, response.status, code, message, body?.details ?? []);
}

export function toProblem(error: unknown): ApiProblem {
  return error instanceof ApiProblem
    ? error
    : new ApiProblem("network", 0, "UNEXPECTED", "Unexpected client error.");
}

export const ACTIVE = new Set<Run["status"]>(["queued", "running", "evaluating"]);
