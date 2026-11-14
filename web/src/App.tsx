import { type FormEvent, useMemo, useState, useSyncExternalStore } from "react";

import { createApi } from "./api/client";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { SessionContext } from "./session";

function subscribe(notify: () => void) {
  window.addEventListener("hashchange", notify);
  return () => window.removeEventListener("hashchange", notify);
}
const currentHash = () => window.location.hash;

const RUN_ROUTE = /^#\/runs\/([0-9a-fA-F-]{36})$/;

/** The token lives only in this component's state: not in URLs, storage or logs. */
export function App() {
  const [token, setToken] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const hash = useSyncExternalStore(subscribe, currentHash);
  const api = useMemo(() => createApi(() => token), [token]);
  const session = useMemo(() => ({ api, signOut: () => setToken(null) }), [api]);

  function enter(event: FormEvent) {
    event.preventDefault();
    setToken(draft.trim());
    setDraft("");
  }

  const runId = RUN_ROUTE.exec(hash)?.[1];

  return (
    <>
      <header>
        <h1>
          <a href="#/">AgentCrash Lab</a>
        </h1>
        <span>Local mock mode</span>
        {token && <button onClick={session.signOut}>Forget token</button>}
      </header>
      <main>
        {!token ? (
          <form onSubmit={enter} className="form" aria-labelledby="token-title">
            <h2 id="token-title">Local API token</h2>
            <p>
              Paste the token from <code>.local/token</code>. It is kept in memory only and is gone
              after a refresh.
            </p>
            <label className="field">
              <span>Token</span>
              <input
                type="password"
                autoComplete="off"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                required
              />
            </label>
            <button type="submit">Use token</button>
          </form>
        ) : (
          <SessionContext.Provider value={session}>
            {runId ? <RunPage key={runId} runId={runId} /> : <RunsPage />}
          </SessionContext.Provider>
        )}
      </main>
    </>
  );
}
