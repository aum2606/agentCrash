import { type FormEvent, useCallback, useState } from "react";

import { type Api, type ApiProblem, type ExportJob, type ReplayJob, type Run, call, toProblem } from "../api/client";
import { Field, Problem } from "../components";
import { usePoll } from "../usePoll";

const key = () => ({ "Idempotency-Key": crypto.randomUUID() });
const unfinished = (j: { status: string }) => (j.status === "queued" || j.status === "running" ? 1000 : null);

// shortcut: the job request bodies are not in the OpenAPI document, so the generated client types
// them as absent; cast until they become request models in the API source.
const asBody = (body: object) => body as never;

function ReplayResult({ api, id }: { api: Api; id: string }) {
  const load = useCallback(
    () => call(api.GET("/api/v1/replays/{replay_id}", { params: { path: { replay_id: id } } })),
    [api, id],
  );
  const { data: r, error, reload } = usePoll<ReplayJob>(load, unfinished);
  return (
    <div role="status" aria-live="polite">
      {error && <Problem error={error} retry={reload} />}
      {r && (
        <>
          <p>
            Strict replay {r.id}: <strong>{r.status}</strong>
            {r.consumed_events !== null && r.total_events !== null && ` (${r.consumed_events} of ${r.total_events} recorded events)`}
          </p>
          {r.status === "completed" && !r.divergence && (
            <p>The agent matched the recording exactly. No tool or model was called.</p>
          )}
          {r.status === "completed" && r.divergence && (
            <>
              <p>The agent diverged from the recording. First difference:</p>
              <pre>{JSON.stringify(r.divergence, null, 2)}</pre>
            </>
          )}
          {r.status === "error" && <p>The replay could not run: {r.error_code}</p>}
        </>
      )}
    </div>
  );
}

function ExportResult({ api, id }: { api: Api; id: string }) {
  const load = useCallback(
    () => call(api.GET("/api/v1/exports/{export_id}", { params: { path: { export_id: id } } })),
    [api, id],
  );
  const { data: x, error, reload } = usePoll<ExportJob>(load, unfinished);
  return (
    <div role="status" aria-live="polite">
      {error && <Problem error={error} retry={reload} />}
      {x && (
        <>
          <p>
            Export {x.id}: <strong>{x.status}</strong>
            {x.pin && " (pinned)"}
          </p>
          {x.status === "completed" && x.artifact_hash && (
            <p>
              Bundle <code>{x.artifact_hash}.acbundle</code> is in the local artifact directory. The web page
              cannot download it; open it offline with <code>agentcrash playback --bundle &lt;file&gt;</code> or{" "}
              <code>agentcrash replay --strict --bundle &lt;file&gt;</code>.
            </p>
          )}
          {x.status === "error" && <p>The export failed: {x.error_code}</p>}
        </>
      )}
    </div>
  );
}

/** Rerun (a new execution), strict replay (recorded responses only) and export, for one closed attempt. */
export function Actions({ api, run, attemptId }: { api: Api; run: Run; attemptId: string }) {
  const [seed, setSeed] = useState("");
  const [pin, setPin] = useState(true);
  const [busy, setBusy] = useState<string>();
  const [problem, setProblem] = useState<ApiProblem>();
  const [replayId, setReplayId] = useState<string>();
  const [exportId, setExportId] = useState<string>();

  async function act(name: string, work: () => Promise<void>) {
    setBusy(name);
    setProblem(undefined);
    try {
      await work();
    } catch (e) {
      setProblem(toProblem(e));
    } finally {
      setBusy(undefined);
    }
  }
  const path = { run_id: run.id };

  const rerun = (event: FormEvent) => {
    event.preventDefault();
    void act("rerun", async () => {
      const created = await call(
        api.POST("/api/v1/runs/{run_id}/reruns", {
          params: { path, header: key() },
          body: asBody(seed.trim() ? { seed: seed.trim() } : {}),
        }),
      );
      window.location.hash = `#/runs/${created.id}`;
    });
  };

  return (
    <section aria-labelledby="actions-title">
      <h3 id="actions-title">Actions</h3>
      {problem && <Problem error={problem} />}

      <form className="form" onSubmit={rerun} aria-label="Rerun">
        <p>
          <strong>Fresh rerun:</strong> a new independent run of the same scenario and agent. The agent runs
          again against the tools, so the outcome can differ.
        </p>
        <Field label="Seed (optional, leave empty to reuse this run's seed)">
          <input value={seed} onChange={(e) => setSeed(e.target.value)} inputMode="numeric" autoComplete="off" />
        </Field>
        <button type="submit" disabled={busy !== undefined}>
          {busy === "rerun" ? "Starting…" : "Start rerun"}
        </button>
      </form>

      <div className="form">
        <p>
          <strong>Strict replay:</strong> feeds the recorded tool responses back to the agent. No real tool
          or model effects. Replay capability of this run: {run.replay_capability}.
        </p>
        <button
          disabled={busy !== undefined}
          onClick={() =>
            void act("replay", async () => {
              const job = await call(
                api.POST("/api/v1/runs/{run_id}/replays", {
                  params: { path, header: key() },
                  body: asBody({ mode: "strict", attempt_id: attemptId }),
                }),
              );
              setReplayId(job.id);
            })
          }
        >
          {busy === "replay" ? "Starting…" : "Start strict replay"}
        </button>
        {replayId && <ReplayResult key={replayId} api={api} id={replayId} />}
      </div>

      <div className="form">
        <p>
          <strong>Export:</strong> writes a self-contained bundle of this attempt.
        </p>
        <label>
          <input type="checkbox" checked={pin} onChange={(e) => setPin(e.target.checked)} /> Pin the bundle
        </label>{" "}
        <button
          disabled={busy !== undefined}
          onClick={() =>
            void act("export", async () => {
              const job = await call(
                api.POST("/api/v1/runs/{run_id}/exports", {
                  params: { path, header: key() },
                  body: asBody({ attempt_id: attemptId, pin }),
                }),
              );
              setExportId(job.id);
            })
          }
        >
          {busy === "export" ? "Starting…" : "Export bundle"}
        </button>
        {exportId && <ExportResult key={exportId} api={api} id={exportId} />}
      </div>
    </section>
  );
}
