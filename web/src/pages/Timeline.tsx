import { useCallback, useState } from "react";

import { type Api, ApiProblem, type EventRecord, type Evaluation, call } from "../api/client";
import { Problem } from "../components";
import { usePoll } from "../usePoll";

const PAGE = 200; // server maximum (CONTRACTS section 2)

async function loadAll(api: Api, runId: string, attemptId: string) {
  const events: EventRecord[] = [];
  let after = 0;
  for (;;) {
    const page = await call(
      api.GET("/api/v1/runs/{run_id}/events", {
        params: {
          path: { run_id: runId },
          query: { attempt_id: attemptId, after_seq: String(after), limit: String(PAGE) },
        },
      }),
    );
    events.push(...page.items);
    if (!page.has_more) return { events, closed: page.attempt_closed };
    // A cursor that does not advance would loop forever against a buggy or hostile API.
    if (page.next_after_seq <= after) throw new ApiProblem("api", 200, "BAD_CURSOR", "The events cursor did not advance.");
    after = page.next_after_seq;
  }
}

const text = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

/** What the tool actually returned versus what the fault engine rewrote it from. */
function Hashes({ e }: { e: EventRecord }) {
  const returned = e.payload["body_ref"];
  const original = e.payload["original_body_ref"];
  if (returned === undefined && original === undefined) return null;
  return (
    <dl className="facts">
      {original !== undefined && (
        <>
          <dt>Original body (before fault)</dt>
          <dd>
            <code>{text(original)}</code>
          </dd>
        </>
      )}
      {returned !== undefined && (
        <>
          <dt>{original !== undefined ? "Returned body (mutated)" : "Returned body"}</dt>
          <dd>
            <code>{text(returned)}</code>
          </dd>
        </>
      )}
    </dl>
  );
}

function summary(e: EventRecord): string {
  const p = e.payload;
  if (e.type === "fault.decided")
    return `${text(p["effect"])} on ${text(p["tool"])} (rule ${text(p["rule_id"])}): ${p["applied"] ? "applied" : "not applied"}`;
  if (e.type === "tool.requested" || e.type === "policy.decided")
    return [p["tool"], p["decision"]].filter((x) => x !== undefined).map(text).join(" ");
  if (e.type === "tool.completed") return `${text(p["tool"])} → ${text(p["status"])}`;
  return "";
}

function EventRow({ e, focused }: { e: EventRecord; focused: boolean }) {
  const injected = e.type === "fault.decided" && e.payload["applied"] === true;
  return (
    <li id={`event-${e.seq}`} className={focused ? "event focused" : "event"}>
      <p>
        <strong>#{e.seq}</strong> {e.type}
        {injected && <span className="badge"> fault injected</span>}
        {focused && <span className="badge"> cited by a grade</span>}
        {e.call_id && ` · ${e.call_id}`} · logical {e.logical_time_ms} ms · {e.wall_time}
      </p>
      {summary(e) && <p>{summary(e)}</p>}
      <Hashes e={e} />
      <details>
        <summary>Payload</summary>
        <pre>{JSON.stringify(e.payload, null, 2)}</pre>
      </details>
    </li>
  );
}

function Grades({ ev, onCite }: { ev: Evaluation; onCite: (seq: number) => void }) {
  return (
    <section aria-labelledby="eval-title">
      <h3 id="eval-title">Evaluation</h3>
      <p>
        Verdict: <strong>{ev.verdict}</strong>
        {ev.primary_reason && ` · primary reason ${ev.primary_reason}`}
      </p>
      {ev.findings.length > 0 && (
        <ul>
          {ev.findings.map((f, i) => (
            <li key={i}>
              {f.code} ({f.source}){f.detail ? `: ${f.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
      <table>
        <thead>
          <tr>
            <th>Grader</th>
            <th>Result</th>
            <th>Reason</th>
            <th>Expected</th>
            <th>Observed</th>
            <th>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {ev.grades.map((g) => (
            <tr key={g.grader_id}>
              <td>
                {g.grader_id} v{g.grader_version} {g.required ? "(required)" : "(optional)"}
              </td>
              <td>{g.result}</td>
              <td>{g.reason_code}</td>
              <td>{JSON.stringify(g.expected)}</td>
              <td>{JSON.stringify(g.observed)}</td>
              <td>
                {(g.evidence ?? []).map((r, i) => {
                  const seq = r.kind === "event" ? Number(r.ref) : NaN;
                  return Number.isInteger(seq) ? (
                    <button key={i} onClick={() => onCite(seq)}>
                      Show event #{seq}
                    </button>
                  ) : (
                    <span key={i}>
                      {r.kind} {r.ref}
                      {r.path ? ` ${r.path}` : ""}{" "}
                    </span>
                  );
                })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function Timeline({
  api,
  runId,
  attemptId,
  active,
}: {
  api: Api;
  runId: string;
  attemptId: string;
  active: boolean;
}) {
  const load = useCallback(async () => {
    const [stream, evals] = await Promise.all([
      loadAll(api, runId, attemptId),
      call(
        api.GET("/api/v1/runs/{run_id}/evaluations", {
          params: { path: { run_id: runId }, query: { attempt_id: attemptId } },
        }),
      ),
    ]);
    return { ...stream, evaluation: evals.items.at(-1) };
  }, [api, runId, attemptId]);
  // Poll while the run can still change; stop once the attempt is closed and the run is idle.
  const delay = useCallback(() => (active ? 1000 : null), [active]);
  const { data, error, loading, reload } = usePoll(load, delay);
  const [cited, setCited] = useState<number>();

  function cite(seq: number) {
    setCited(seq);
    document.getElementById(`event-${seq}`)?.scrollIntoView?.({ block: "center" });
  }

  return (
    <section aria-labelledby="timeline-title">
      <h3 id="timeline-title">Timeline (attempt {attemptId})</h3>
      {loading && <p role="status">Loading timeline…</p>}
      {error && <Problem error={error} retry={reload} />}
      {data && (
        <>
          {data.evaluation ? (
            <Grades ev={data.evaluation} onCite={cite} />
          ) : (
            <p>Not evaluated yet.</p>
          )}
          {data.events.length === 0 ? (
            <p>No events recorded yet.</p>
          ) : (
            <ol className="events">
              {data.events.map((e) => (
                <EventRow key={e.seq} e={e} focused={e.seq === cited} />
              ))}
            </ol>
          )}
          {!data.closed && <p>This attempt is still open; events may be added.</p>}
        </>
      )}
    </section>
  );
}
