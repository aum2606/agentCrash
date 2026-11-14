import { useCallback, useState } from "react";

import { ACTIVE, type ApiProblem, type Run, call, toProblem } from "../api/client";
import { Pair, Problem, Status } from "../components";
import { useSession } from "../session";
import { usePoll } from "../usePoll";
import { Timeline } from "./Timeline";

// One second while the run can still change, five when it cannot (CONTRACTS section 2).
const delay = (run: Run) => (ACTIVE.has(run.status) ? 1000 : 5000);

export function RunPage({ runId }: { runId: string }) {
  const { api } = useSession();
  const load = useCallback(
    () => call(api.GET("/api/v1/runs/{run_id}", { params: { path: { run_id: runId } } })),
    [api, runId],
  );
  const { data: run, error, loading, reload } = usePoll(load, delay);
  const [cancelError, setCancelError] = useState<ApiProblem>();
  const [cancelling, setCancelling] = useState(false);

  async function cancel() {
    setCancelling(true);
    setCancelError(undefined);
    try {
      await call(
        api.POST("/api/v1/runs/{run_id}/cancel", {
          params: { path: { run_id: runId } },
        }),
      );
      reload();
    } catch (e) {
      setCancelError(toProblem(e));
    } finally {
      setCancelling(false);
    }
  }

  const attemptId = run?.selected_attempt_id ?? run?.attempts.at(-1)?.id;

  return (
    <section aria-labelledby="run-title">
      <p>
        <a href="#/">← All runs</a>
      </p>
      <h2 id="run-title">Run {runId}</h2>
      {loading && <p role="status">Loading run…</p>}
      {error && <Problem error={error} retry={reload} />}
      {run && (
        <>
          <dl className="facts">
            <Pair name="Status">
              <Status value={run.status} />
              {ACTIVE.has(run.status) && " (updating every second)"}
            </Pair>
            <Pair name="Verdict">{run.verdict ?? "none yet"}</Pair>
            {run.error_code && <Pair name="Error code">{run.error_code}</Pair>}
            <Pair name="Scenario">
              {run.scenario.id} v{run.scenario.version}
            </Pair>
            <Pair name="Agent">
              {run.agent.id} v{run.agent.version}
            </Pair>
            <Pair name="Seed">{run.seed}</Pair>
            <Pair name="Manifest hash">
              <code>{run.manifest_hash}</code>
            </Pair>
            <Pair name="Attempts">
              {run.attempts.length === 0
                ? "none yet"
                : run.attempts.map((a) => `#${a.ordinal} ${a.termination ?? "in progress"}`).join(", ")}
            </Pair>
            <Pair name="Created">{run.created_at}</Pair>
            <Pair name="Completed">{run.completed_at ?? "not finished"}</Pair>
          </dl>
          {ACTIVE.has(run.status) && (
            <button onClick={() => void cancel()} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel run"}
            </button>
          )}
          {cancelError && <Problem error={cancelError} />}
          {attemptId && (
            <Timeline api={api} runId={runId} attemptId={attemptId} active={ACTIVE.has(run.status)} />
          )}
        </>
      )}
    </section>
  );
}
