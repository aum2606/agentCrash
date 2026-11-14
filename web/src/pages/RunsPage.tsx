import { type FormEvent, useCallback, useState } from "react";

import {
  ACTIVE,
  ApiProblem,
  type Budgets,
  type Run,
  call,
  toProblem,
} from "../api/client";
import { Field, Problem, Status } from "../components";
import { useSession } from "../session";
import { usePoll } from "../usePoll";

// Upper bounds from CONTRACTS: a UI cannot ask for more than the server allows.
const DEFAULT_BUDGETS: Required<Budgets> = {
  logical_ms: 60000,
  steps: 20,
  tool_calls: 50,
  wall_ms: 120000,
};
const BUDGET_LABELS: Record<keyof Budgets, string> = {
  logical_ms: "Logical time limit (ms)",
  steps: "Agent steps",
  tool_calls: "Tool calls",
  wall_ms: "Wall-clock limit (ms)",
};

const never = () => null;
const listDelay = (runs: Run[]) => (runs.some((r) => ACTIVE.has(r.status)) ? 1000 : 5000);

export function RunsPage() {
  const { api } = useSession();
  const loadScenarios = useCallback(
    async () => (await call(api.GET("/api/v1/scenarios", { params: { query: { limit: "200" } } }))).items,
    [api],
  );
  const loadAgents = useCallback(async () => (await call(api.GET("/api/v1/agents"))).items, [api]);
  const loadRuns = useCallback(
    async () => (await call(api.GET("/api/v1/runs", { params: { query: { limit: "50" } } }))).items,
    [api],
  );
  const scenarios = usePoll(loadScenarios, never);
  const agents = usePoll(loadAgents, never);
  const runs = usePoll(loadRuns, listDelay);

  const [scenarioKey, setScenarioKey] = useState("");
  const [agentKey, setAgentKey] = useState("");
  const [seed, setSeed] = useState("42");
  const [budgets, setBudgets] = useState<Required<Budgets>>(DEFAULT_BUDGETS);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiProblem>();

  const scenarioOptions = scenarios.data ?? [];
  const agentOptions = agents.data ?? [];
  const scenario = scenarioKey || `${scenarioOptions[0]?.id}@${scenarioOptions[0]?.version}`;
  const agent = agentKey || `${agentOptions[0]?.id}@${agentOptions[0]?.version}`;
  const ready = scenarioOptions.length > 0 && agentOptions.length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    const [scenarioId = "", scenarioVersion = "0"] = scenario.split("@");
    const [agentId = "", agentVersion = ""] = agent.split("@");
    setSubmitting(true);
    setSubmitError(undefined);
    try {
      const run = await call(
        api.POST("/api/v1/runs", {
          params: { header: { "Idempotency-Key": crypto.randomUUID() } },
          body: {
            schema_version: "1.0",
            mode: "mock",
            scenario: { id: scenarioId, version: Number(scenarioVersion) },
            agent: { id: agentId, version: agentVersion, config: {} },
            seed,
            budgets,
            labels: {},
          },
        }),
      );
      window.location.hash = `#/runs/${run.id}`;
    } catch (error) {
      setSubmitError(toProblem(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <section aria-labelledby="new-run">
        <h2 id="new-run">New run</h2>
        <p>
          Mock mode only: synthetic invoice tools and deterministic agents. No model provider or
          API key is used.
        </p>
        {(scenarios.error ?? agents.error) && (
          <Problem
            error={(scenarios.error ?? agents.error) as ApiProblem}
            retry={() => {
              scenarios.reload();
              agents.reload();
            }}
          />
        )}
        {(scenarios.loading || agents.loading) && <p role="status">Loading scenarios and agents…</p>}
        {!scenarios.loading && !agents.loading && !scenarios.error && !agents.error && !ready && (
          <p role="status">
            No scenarios or agents are registered. Run <code>agentcrash dev init</code> and reload.
          </p>
        )}
        {ready && (
          <form onSubmit={(e) => void submit(e)} className="form">
            <Field label="Scenario">
              <select value={scenario} onChange={(e) => setScenarioKey(e.target.value)}>
                {scenarioOptions.map((s) => (
                  <option key={`${s.id}@${s.version}`} value={`${s.id}@${s.version}`}>
                    {s.id} v{s.version}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Agent">
              <select value={agent} onChange={(e) => setAgentKey(e.target.value)}>
                {agentOptions.map((a) => (
                  <option key={`${a.id}@${a.version}`} value={`${a.id}@${a.version}`}>
                    {a.id} v{a.version}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Seed">
              <input value={seed} onChange={(e) => setSeed(e.target.value)} required />
            </Field>
            <fieldset>
              <legend>Budgets (maximums set by the server)</legend>
              {(Object.keys(BUDGET_LABELS) as (keyof Budgets)[]).map((key) => (
                <Field key={key} label={BUDGET_LABELS[key]}>
                  <input
                    type="number"
                    min={1}
                    max={DEFAULT_BUDGETS[key]}
                    value={budgets[key]}
                    onChange={(e) => setBudgets({ ...budgets, [key]: Number(e.target.value) })}
                    required
                  />
                </Field>
              ))}
            </fieldset>
            <button type="submit" disabled={submitting}>
              {submitting ? "Submitting…" : "Start run"}
            </button>
            {submitError && <Problem error={submitError} />}
          </form>
        )}
      </section>

      <section aria-labelledby="runs">
        <h2 id="runs">Runs</h2>
        {runs.loading && <p role="status">Loading runs…</p>}
        {runs.error && <Problem error={runs.error} retry={runs.reload} />}
        {runs.data?.length === 0 && <p role="status">No runs yet. Start one above.</p>}
        {runs.data && runs.data.length > 0 && (
          <ul className="cards">
            {runs.data.map((run) => (
              <li key={run.id}>
                <a href={`#/runs/${run.id}`}>
                  {run.scenario.id} v{run.scenario.version}
                </a>
                <span>
                  {run.agent.id} v{run.agent.version}, seed {run.seed}
                </span>
                <span>
                  <Status value={run.status} />
                  {run.verdict ? <> · verdict: {run.verdict}</> : null}
                </span>
                <time dateTime={run.created_at}>{run.created_at}</time>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
