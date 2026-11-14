import { useCallback } from "react";

import { type Experiment, type Ratio, type Report, call } from "../api/client";
import { Pair, Problem, Status } from "../components";
import { useSession } from "../session";
import { usePoll } from "../usePoll";

/** Exact integer ratio first; the decimal only when the denominator is not zero. */
const ratio = (r: Ratio) => (r.value === null ? `${r.n}/${r.d} (no samples)` : `${r.n}/${r.d} (${r.value})`);

const AGENT_COLUMNS = [
  ["submitted", "Submitted"],
  ["passed", "Passed"],
  ["failed", "Failed"],
  ["inconclusive", "Inconclusive"],
  ["errored", "Errored"],
  ["cancelled", "Cancelled"],
  ["unfinished", "Unfinished"],
] as const;

const ROLES = ["baseline", "candidate"] as const;

function Side({ side, runId }: { side: Report["pairs"][number]["baseline"]; runId?: string }) {
  if (!side) return <>missing</>;
  const label = side.verdict ?? side.status;
  return (
    <>
      {runId ? <a href={`#/runs/${runId}`}>{label}</a> : label}
      {side.primary_reason && ` · ${side.primary_reason}`}
      {side.error_code && ` · ${side.error_code}`}
    </>
  );
}

function Matrix({ report, experiment }: { report: Report; experiment: Experiment }) {
  const key = (s: string, v: number, seed: string, t: number, role: string) => `${s}@${v}/${seed}/${t}/${role}`;
  const runs = new Map(
    experiment.slots.map((s) => [key(s.scenario_id, s.scenario_version, s.seed, s.trial, s.role), s.run_id]),
  );
  return (
    <table>
      <caption>Paired results (verdict, else lifecycle status; each links to its run)</caption>
      <thead>
        <tr>
          <th>Scenario</th>
          <th>Seed</th>
          <th>Trial</th>
          <th>Baseline</th>
          <th>Candidate</th>
          <th>Pair</th>
        </tr>
      </thead>
      <tbody>
        {report.pairs.map((p) => (
          <tr key={key(p.scenario_id, p.scenario_version, p.seed, p.trial, "")}>
            <td>
              {p.scenario_id} v{p.scenario_version}
            </td>
            <td>{p.seed}</td>
            <td>{p.trial}</td>
            {ROLES.map((role) => (
              <td key={role}>
                <Side
                  side={p[role]}
                  runId={runs.get(key(p.scenario_id, p.scenario_version, p.seed, p.trial, role))}
                />
              </td>
            ))}
            <td>{p.complete ? "complete" : "incomplete"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ReportView({ report, experiment }: { report: Report; experiment: Experiment }) {
  return (
    <>
      <section aria-labelledby="gate-title">
        <h3 id="gate-title">Gate ({report.gate_policy_id})</h3>
        <p>
          Gate: <strong>{report.gate.status}</strong> · {report.complete_pairs} of {report.expected_pairs} pairs
          complete
        </p>
        {report.gate.reasons.length > 0 && (
          <ul>
            {report.gate.reasons.map((r, i) => (
              <li key={i}>
                {r.code}: {r.detail}
              </li>
            ))}
          </ul>
        )}
      </section>

      <table>
        <caption>Results by agent</caption>
        <thead>
          <tr>
            <th>Agent role</th>
            {AGENT_COLUMNS.map(([, label]) => (
              <th key={label}>{label}</th>
            ))}
            <th>Pass rate</th>
            <th>Pass rate (conclusive only)</th>
          </tr>
        </thead>
        <tbody>
          {ROLES.map((role) => {
            const c = report.counts_by_agent[role];
            return (
              <tr key={role}>
                <td>{role}</td>
                {AGENT_COLUMNS.map(([field]) => (
                  <td key={field}>{c?.[field]}</td>
                ))}
                <td>{c && ratio(c.pass_rate)}</td>
                <td>{c && ratio(c.conclusive_pass_rate)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <table>
        <caption>Paired pass-rate change (candidate minus baseline, complete pairs only)</caption>
        <thead>
          <tr>
            <th>Scenario</th>
            <th>Pairs</th>
            <th>Baseline passes</th>
            <th>Candidate passes</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          {report.paired_completion_delta.map((d) => (
            <tr key={d.scenario_id ?? "all"}>
              <td>{d.scenario_id ?? "all scenarios"}</td>
              <td>{d.pairs}</td>
              <td>{d.baseline_passes}</td>
              <td>{d.candidate_passes}</td>
              <td>{ratio(d.delta)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <Matrix report={report} experiment={experiment} />

      {report.missing_pairs.length > 0 && (
        <section aria-labelledby="missing-title">
          <h3 id="missing-title">Missing pairs</h3>
          <ul>
            {report.missing_pairs.map((m, i) => (
              <li key={i}>
                {m.scenario_id} v{m.scenario_version}, seed {m.seed}, trial {m.trial}: missing{" "}
                {m.missing.join(" and ")} ({m.reasons.join(", ")})
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.baseline_failures.length > 0 && (
        <p>
          Baseline failed in {report.baseline_failures.length} pair(s): the gate counts those against the
          baseline, not the candidate.
        </p>
      )}

      <table>
        <caption>Safety findings (number of runs with each finding)</caption>
        <thead>
          <tr>
            <th>Agent role</th>
            <th>Attempted unsafe</th>
            <th>Committed unsafe</th>
            <th>Duplicate write</th>
            <th>Stale source</th>
            <th>Trace incomplete</th>
          </tr>
        </thead>
        <tbody>
          {ROLES.map((role) => {
            const s = report.safety_counts[role];
            return (
              <tr key={role}>
                <td>{role}</td>
                <td>{s?.attempted_unsafe}</td>
                <td>{s?.committed_unsafe}</td>
                <td>{s?.duplicate_write}</td>
                <td>{s?.stale_source}</td>
                <td>{s?.trace_incomplete}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <table>
        <caption>Fault exposure and recovery</caption>
        <thead>
          <tr>
            <th>Scenario</th>
            <th>Agent role</th>
            <th>Runs</th>
            <th>Exposed</th>
            <th>Applications</th>
            <th>Recovered</th>
          </tr>
        </thead>
        <tbody>
          {report.fault_exposure_by_scenario.map((x) => (
            <tr key={`${x.scenario_id}/${x.role}`}>
              <td>{x.scenario_id}</td>
              <td>{x.role}</td>
              <td>{x.runs}</td>
              <td>{ratio(x.exposure_rate)}</td>
              <td>{x.applications}</td>
              <td>
                {x.recovered} of {x.recovery_eligible} eligible ({ratio(x.recovery_rate)})
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <table>
        <caption>Latency (mock logical time and wall time, ms)</caption>
        <thead>
          <tr>
            <th>Agent role</th>
            <th>Clock</th>
            <th>Samples</th>
            <th>p50</th>
            <th>p95</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>
          {ROLES.flatMap((role) =>
            (["logical_ms", "wall_ms"] as const).map((clock) => {
              const s = report.latency_summary[role]?.[clock];
              return (
                <tr key={`${role}/${clock}`}>
                  <td>{role}</td>
                  <td>{clock === "logical_ms" ? "logical" : "wall"}</td>
                  <td>{s?.count}</td>
                  <td>{s?.p50 ?? "n/a"}</td>
                  <td>{s?.p95 ?? "n/a"}</td>
                  <td>{s?.warning === "SMALL_SAMPLE" ? "small sample: no confidence claim" : ""}</td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
      <p>
        Model requests, tokens and cost are not measured for mock runs. Grader set{" "}
        <code>{report.grader_set_hash ?? "none"}</code>.
      </p>
    </>
  );
}

export function ExperimentPage({ experimentId }: { experimentId: string }) {
  const { api } = useSession();
  const load = useCallback(async () => {
    const path = { experiment_id: experimentId };
    const [experiment, report] = await Promise.all([
      call(api.GET("/api/v1/experiments/{experiment_id}", { params: { path } })),
      // partial=true so a running experiment shows an honest "incomplete" report, not a 409.
      call(
        api.GET("/api/v1/experiments/{experiment_id}/report", {
          params: { path, query: { partial: "true" } },
        }),
      ),
    ]);
    return { experiment, report };
  }, [api, experimentId]);
  const delay = useCallback(
    (d: { experiment: Experiment }) => (d.experiment.status === "running" ? 1000 : null),
    [],
  );
  const { data, error, loading, reload } = usePoll(load, delay);
  const e = data?.experiment;

  return (
    <section aria-labelledby="exp-title">
      <p>
        <a href="#/">← All runs</a>
      </p>
      <h2 id="exp-title">Experiment {experimentId}</h2>
      {loading && <p role="status">Loading experiment…</p>}
      {error && <Problem error={error} retry={reload} />}
      {e && data && (
        <>
          <dl className="facts">
            <Pair name="Name">{e.name}</Pair>
            <Pair name="Status">
              <Status value={e.status} />
              {e.status === "running" && " (updating every second)"}
            </Pair>
            <Pair name="Runs expected">{e.expected_runs}</Pair>
            {ROLES.map((role) => {
              const p = e.progress[role];
              return (
                <Pair key={role} name={`Progress (${role})`}>
                  {p
                    ? `${p.completed + p.error} of ${p.total} finished (${p.queued} queued, ${p.running} running, ${p.evaluating} evaluating, ${p.error} errored)`
                    : "none"}
                </Pair>
              );
            })}
            <Pair name="Config hash">
              <code>{e.config_hash}</code>
            </Pair>
            <Pair name="Created">{e.created_at}</Pair>
          </dl>
          <ReportView report={data.report} experiment={e} />
        </>
      )}
    </section>
  );
}
