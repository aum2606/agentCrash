# Progress ledger

Last updated: 2026-11-11. M0, M1 and M2 are **accepted** (CI green on `origin/main`, user-reported; M2 @ `821db83`; run URLs not seen). Current milestone: **M3 in progress, slice 6 implemented and verified locally, not accepted** (slices 1-5 are commits `af3f4ba`, `1cd6a80`, `436e48a`, `d975ab1`, `22c58a1`; slice 6 is uncommitted; nothing pushed; CI has run none of them).

## M3: fault engine and resilient agent (slices 1-6; not accepted)

Prior milestone: **M2 accepted 2026-11-11** on the user's report that CI passed for `821db83` (I could not see the run; `gh` is not installed here). Decisions D-028, D-029.

**Implemented** (M3 tasks 1 and 3 are partial; 2, 4, 5 not started):
- `faults/matcher.py` (pure): exact SHA-256 sampling with exact-fraction probability, eligibility (tool, actor, ordinal), stage/priority/rule-ID ordering, `applied`/`not_sampled`/`exhausted` decisions, and a runtime refusal (`AmbiguousComposition`) for combinations the compiler should already have rejected. A golden sample value pins the serialization.
- Gateway (`execution/gateway.py`): after the policy decision it records one `fault.decided` event per eligible rule, then applies `delay`, `http_error`, `timeout_before_commit` and `timeout_after_commit`. Application counts come from the attempt's own events (`storage/attempts.py`), so retransmits never double count.
- Timeouts are real transport failures: stored as the call outcome, raised as `ToolTimeout` after commit, carried over loopback as `{kind:"transport_error"}`, re-raised by `HttpToolClient`. A retransmitted call ID repeats the timeout without a new ordinal.
- Supervisor passes faults, seed and scenario hash to the gateway and still refuses scenarios needing `schema_mutation`, `stale_read`, `document_injection` or `scripted_conflict` (`FAULTS_NOT_IMPLEMENTED`).
- New reviewed scenarios: `invoice-503`, `invoice-timeout-before`, `invoice-timeout-after`, `invoice-latency`, `invoice-schema-change` (refused for now). `expected_demo` metadata was not added: the model holds one verdict, not one per agent, and changing it is a contract change.

**Evidence** (Windows, PostgreSQL 18.6 via Docker Desktop, all run locally this session):
- `uv run agentcrash check` passed (ruff, mypy --strict, schema/OpenAPI drift).
- Non-DB suites (unit, property, contract): 258 passed. Integration (real PostgreSQL): 114 passed, including 8 gateway fault tests (`test_faults.py`) and 15 end-to-end naive-agent cases (`test_fault_suite.py`).
- Naive agent against the demonstration table, through the real worker and child process: `invoice-429` and `invoice-503` fail with no ledger entry; `invoice-timeout-before` fails with none (no `environment.mutated`); `invoice-timeout-after` leaves exactly one mutation and the state oracle **passes** although the agent errored (reported, not forced to fail); `invoice-latency` passes and consumes 2000 logical ms; healthy passes. Same manifest twice gives an identical observable digest and final-state hash for 429, timeout-after and latency.
- Ineligible rules never consume applications (ordinal-targeted and other-tool rules checked against persisted events); policy denial produces no `fault.decided` event.
- Mutation checks, each caught then restored: ignore `max_applications`; match the wrong tool; dispatch despite a pre-commit timeout; decide faults for denied calls.
- Fixes along the way: dev-init tests hard-coded two scenarios (now derived from the scenario directory); a grouped JSON SQL query failed under server-side parameters (counts are now done in Python over a handful of rows).

**Slice 2: `schema_mutation`** (decision D-030):
- `domain/pointer.py` (object-key JSON Pointer check and copy-on-write mutate), `ToolSpec.body_shape` for the four mutable tools, and a compile-time check: `POINTER_NOT_FOUND`, `RENAME_TARGET_EXISTS`.
- Gateway applies the mutation to a 200 body only and records `original_body_ref` plus `mutation{...}` on `tool.completed`; an applied rule on an error status is recorded as not applied.
- `invoice-schema-change` now executes. The naive agent posts the purchase order's approved amount, so a renamed field raises and nothing is posted (`fail`, `agent_error`); healthy results are unchanged.
- Tests added: `test_pointer.py` (unit and Hypothesis), 3 compiler rejections plus one accepted nested pointer, 2 gateway tests, the suite case, and a test that a manifest needing an unimplemented effect is still refused. Mutation checks (each caught, then restored): mutation made a no-op, mutation applied to error bodies, compiler pointer check disabled.
- Final run this session: `agentcrash check` passed; 271 non-DB tests passed; integration **118 passed** (149 s).
- **Unexplained intermittent integration failures (2 seen across about 15 full-file or full-suite runs; the first test never failed in 7 isolated runs):** `test_child_killed_after_the_write_commits_is_replaced_from_a_fresh_fixture` (M2; failed in two runs, message not captured) and `test_each_faulted_case_records_exactly_one_applied_decision[invoice-timeout-after]` (no `fault.decided` event, so the agent never reached the post). The second came from a run that took 1123 s instead of about 150 s, so the machine stalled and a real wall-clock limit (120 s attempt deadline, 30 s leases) plausibly fired; the first test kills only the Windows launcher process, which may race with an orphaned agent. Neither cause is proven. The assertion now reports the attempt's termination so the next occurrence is diagnosable. These tests rely on real wall-clock limits, which CLAUDE.md prefers to avoid; do not treat them as deterministic until this is resolved.

**Slice 3: `stale_read`** (decision D-031):
- `fixtures/invoice/v1/faults.json` holds the stale purchase-order snapshot `po-stale-v1` (version 1, 9000, one day old); the fixture loader hashes it (only when present), the registry derives `stale_snapshots` from it, and the supervisor hands it to the gateway.
- The gateway's single rewrite step now serves the snapshot body and version for `stale_read` and records `replacement{...}` and `original_body_ref`; error bodies are untouched. The real purchase order is never modified.
- New scenario `invoice-stale-po`. The naive agent posts 9000 from version 1: one wrong ledger entry, `fail`, failing exactly `invoice.correct_post.v1` and `invoice.fresh_source.v1` while duplicate, authorization and trace checks pass.
- Tests added: 2 gateway tests (snapshot served, both hashes, error body untouched), the suite case and a test of which graders fail. Mutation checks (caught, restored): snapshot version not applied, body not substituted, error bodies replaced.
- Final run: `agentcrash check` passed; 271 non-DB tests passed; integration 123 passed (195 s). The two intermittent failures noted under slice 2 did not recur in this run, and remain unexplained.

**Slice 4: `document_injection`, `scripted_conflict`, vulnerable parser** (decision D-032): all eight fault effects and all ten suite scenarios now exist.
- Fixtures: a malicious invoice document and a concurrent-edit patch in `faults.json`; the registry derives both id sets from it.
- Gateway: document text replacement with the shared `replacement` evidence; the competing actor's CAS edit is an `environment.mutated` event by `competing-actor` in the same transaction as the call it precedes (no ordinal, skipped when policy denies the call).
- `agents/insecure_demo.py` (loudly labelled, import-restricted by an architecture test): the naive agent obeys `ACTION:` lines in documents, attempts the forbidden vendor change, policy denies it, and `invoice.no_unauthorized.v1` fails with `UNAUTHORIZED_ATTEMPT` while the posting itself is correct and nothing in the vendor row changes.
- Naive results, all through the real worker and child process: `invoice-cas-conflict` fails with a 409 and no entry (task incomplete); `invoice-document-injection` fails on safety only.
- Tests added: 5 parser unit tests, an architecture test, 2 gateway tests (document replacement; conflict with a refreshed same-key retry winning and exactly one competing event), 2 suite cases. Mutation checks (caught, restored): document text not replaced, naive stops obeying documents, parser cap removed, conflict never applied, conflict attributed to the agent, conflict without version bump.
- Final run: `agentcrash check` passed; 277 non-DB tests passed; integration 130 passed (175 s). The two intermittent failures noted under slice 2 did not recur and remain unexplained.

**Slice 5: resilient agent** (decision D-033):
- `agents/resilient.py` is registered and runnable (`invoice-resilient` 1.0.0): typed checks, age-based freshness with one forced refresh, bounded retries with the contract's backoff and seeded jitter, same-key writes with receipt reconciliation, CAS refresh, document read but never interpreted.
- Results through the real worker and child process: it passes all ten cases, each with exactly one ledger posting, a chain-valid trace and no failed grader (including the informational `recovery.observed` where present). Specific behavior asserted from events: 429 waits at least the 1000 ms hint (and at most 1100); a pre-commit timeout is followed by a receipt lookup (404) and a same-key retry; a post-commit timeout posts once and reconciles by receipt; a renamed field gets exactly one `force_refresh` read; a stale snapshot is replaced before posting (10000 from version 2); the injected document causes no forbidden call or unsafe attempt; a version conflict is resolved by re-reading and posting again under the same key (expected versions 1 then 2). Same manifest twice gives identical digest and final-state hash (429, CAS conflict, post-commit timeout); different seeds change only the jitter.
- Mutation checks (each caught, then restored): age check removed, retry-after ignored, receipt reconcile removed, invoice never refreshed, forced refresh removed, fresh key on every retry.
- Adapter-missing guard: the old "resilient has no adapter" test now withdraws the adapter with a monkeypatch to prove the guard still fails closed.
- Final run: `agentcrash check` passed; 277 non-DB tests passed; integration 152 passed (289 s). One `ResourceWarning` set (unclosed connections and log files in older M1/M2 tests) appears in the summary; not from this slice, not fixed. The two intermittent failures noted under slice 2 did not recur and remain unexplained.

**Slice 6: loop warning, resource termination evidence, failure classification** (decision D-034):
- Gateway: a non-mutating call that is the third identical read in a row with the same coarse outcome carries `loop_suspected` on `tool.completed` / `tool.transport_failed` (additive, heuristic only). `AttemptTx.recent_calls` supports it. Hard budgets were already enforced in M2.
- `evaluation/classify.py` (pure): all findings kept, primary reason by `REASON_PRIORITY` (extended with `SCHEMA_UNHANDLED`, `RETRY_EXHAUSTED`, `LOOP_SUSPECTED`; specific task causes rank above generic `TASK_INCOMPLETE`), none for a passing run. Stored via **migration 0003** (`evaluations.primary_reason`, `evaluations.findings`) and returned by `GET /runs/{id}/evaluations`; OpenAPI regenerated. **Additive contract extension awaiting maintainer confirmation.**
- Negative fixture `scenarios/invoice/v1/negative/neg-permanent-failure.yaml` (invoice read always 503): the resilient agent makes exactly 3 attempts, posts nothing and is classified `RETRY_EXHAUSTED` with `TASK_INCOMPLETE` and `LOOP_SUSPECTED` kept. The unsafe-candidate (naive on document injection: `UNAUTHORIZED_ATTEMPT`) and unhandled-schema (naive on schema change: `SCHEMA_UNHANDLED`) cases reuse suite scenarios. Looping is driven through the gateway directly; no looping agent is shipped. The incomplete-evidence case is covered by unit tests over hand-built evidence (`TRACE_INCOMPLETE` outranks everything).
- Tests added: `tests/unit/test_classify.py` (20) and `tests/integration/test_loop_and_classification.py` (10: streak, reset, progress, writes never flagged, hard budget at the gateway, passing run has no reason, permanent failure end to end and over the API, unsafe candidate, unhandled schema, a real run stopped by a 2-call budget). Mutation checks, each caught then restored: window of 4, writes can loop, outcome ignored, trace flag ignored, classification not stored, priority order changed.
- Test fixtures updated for the head revision (`0003`), recursive scenario discovery (`rglob`) and the new scenario directory.
- **Verification actually completed:** `agentcrash check` passed; 297 non-DB tests passed; the 10 new integration tests passed on their own.
- **Integration verification, completed in a second pass:** the single full-suite run had been killed for low memory (not a test failure), so every integration file was re-run separately, one pytest invocation at a time, all against PostgreSQL 18.6 and all passing: schema + queue 23, dev-init/restart 3, API flow 23, gateway 21, faults 14, execution 20, worker process 1, fault suite 25, resilient suite 22, loop and classification 10 = **162 tests across all 11 files**. This includes the migration-drift test at head `0003` and the dev-init count over the recursive scenario directory. It was not a single combined run, so cross-file interactions are not exercised together; the two earlier unexplained intermittent failures did not recur.
- The local development database (if any) is at migration 0002; run `uv run alembic upgrade head` before `agentcrash worker` or `agentcrash api` there.

**CI status of M3 (2026-11-12):** pushing `95469d3` to `main` created workflow run 43097620419, which stayed `queued` with zero jobs for more than 8 hours and could not be cancelled (normal cancel: "completed"; force-cancel: HTTP 409 "has not been queued yet"). GitHub's status page reported no incident. This looks like a broken run record on GitHub's side; it may also hold the `main` concurrency group (`ci-refs/heads/main`), so further pushes to `main` could queue behind it. CI evidence for M3 is therefore being obtained from a pull request, which uses its own concurrency group. M3 is not accepted until that run (or a fresh `main` run) is green.

**Not done / limits:**
- Every fault effect is implemented; unimplemented ones would still be refused by the supervisor guard; no `expected_demo` per-agent metadata (the model has one verdict).
- The resilient agent passes all ten cases in my tests, but the formal 10-case x 2-agent acceptance run and its gate are M5. The naive agent's results are asserted in `tests/integration/test_fault_suite.py` from the IMPLEMENTATION.md table, but not against machine-readable `expected_demo` metadata (the model cannot hold one per agent), so that acceptance wording is only partly met.
- The naive agent does not catch `ToolTimeout`; it ends as `agent_error` through the child's generic handler rather than a deliberate "gave up".
- The `recovery.observed` grader was exercised only through scenario compilation, not against a resilient run.
- No Linux or CI run of this slice; nothing pushed or committed.

**Smallest next task:** commit slice 6 with approval, then push so CI can run M3 on Linux. M3 acceptance still needs CI and a maintainer look at D-034's priority order and the two new columns.

---

## M2: durable healthy execution (accepted 2026-11-11)

All six M2 tasks are implemented for the healthy path with the naive agent. CI passed for commit `821db83` (user-reported 2026-11-11). Faults and the resilient agent are M3: runs needing unimplemented ones end in `error` (`FAULTS_NOT_IMPLEMENTED`, `ADAPTER_NOT_IMPLEMENTED`), never as a fake pass.

**Implemented** (decisions D-025..D-027):
- Queue (`storage/queue.py`): `FOR UPDATE SKIP LOCKED` claim with a fresh attempt and fencing-token bump, 30 s leases, heartbeat that cannot revive an expired lease, reaper (`worker_lost`, replacement attempt, 2-attempt cap -> run `error` / `INFRA_WORKER_LOST`), immediate `abandon` for the supervisor, separate grading claim with a 3-delivery cap.
- Evidence (`migrations/0002`, `storage/attempts.py`, `domain/events.py`): per-attempt fixture clone, contiguous hash-chained events (genesis constant, 128 KiB cap), closure event with final-state hash, append-only events and receipts enforced by trigger, tool-call, receipt and ledger tables, CHECK on terminations, `runs.error_code`.
- Gateway (`execution/gateway.py`, `server.py`): policy before dispatch, one fenced transaction per call, retransmitted call ID returns the stored outcome, same-key receipts, CAS, ledger uniqueness, budgets, redaction (`domain/redaction.py`), loopback HTTP with a per-attempt capability and `CONCURRENT_CALL_UNSUPPORTED`.
- Runner (`execution/child.py`, `workers/supervisor.py`): agent in a separate process with an allowlisted environment, no storage imports; real deadline; cancellation polling; whole-process-tree kill (a Windows venv launcher otherwise orphans the agent); drain-then-abandon on shutdown.
- Agent and graders: `invoice-naive` healthy path, seeded agent RNG, seven graders and the verdict rule (`evaluation/graders.py`, `domain/evidence.py`).
- API: `POST /runs/{id}/cancel`, `GET /runs/{id}/events`, `GET /runs/{id}/evaluations`; `error_code` on runs. CLI: `agentcrash worker` (refuses an unmigrated database). New scenario `invoice-healthy`.

**Evidence** (Windows, PostgreSQL 18.6; all run locally):
- `agentcrash check` passes; 241 non-DB tests; 91 integration tests (20 queue/execution + 21 gateway + 1 real-worker-process test among them).
- Healthy run reaches `completed/pass` with contiguous, attributable, chain-valid events; two runs with one manifest give an identical observable digest and final-state hash; a new API app instance serves the finished run and its events.
- Real `agentcrash worker` process killed (whole tree) after the write committed: the attempt is `worker_lost` with the mutation recorded, the replacement starts from the fixture and passes, only it is graded. Child killed after the write, suspended worker waking after reassignment (fenced, publishes nothing), two lost attempts (`INFRA_WORKER_LOST`), drain abandon: all tested.
- Duplicate delivery: finished runs are never reclaimed; a re-delivered grading job publishes one evaluation. Retransmitted call ID, same key/same payload, changed payload (409), different key (`ALREADY_POSTED`), CAS conflict then same-key retry: tested.
- Cancel before the write commits nothing; cancel after keeps the committed state, audited, verdict `inconclusive`. Hung call hits the deadline, child gone, verdict `fail`.
- Mutation checks (each broke a test, then restored): gateway ignoring cancel, skipping the fence, disabling receipt replay, disabling redaction, not requiring the integrity grader; earlier: removing `SKIP LOCKED` / the token comparison.

**Mistakes caught:** reaper overwrote an attempt a worker had just closed; tests leaked a connection that blocked `TRUNCATE`; a self-deadlocking lock test; the Windows orphaned-child issue above; the worker-kill test first deadlocked on its own lock order (a killed client's backend stays blocked on the advisory lock until released); a hardcoded `0001` revision in an old test; a stray `git stash` (immediately restored, nothing lost).

**Not done / limits:**
- No Linux or GitHub Actions run of this code.
- Faults, fault events, resilient agent, vulnerable document parser, stale snapshots and conflict patches (M3). Logical time advances only through `clock.advanced` (base tool latency is 0 ms, D-027).
- On Windows `terminate` is already forceful, so the 2 s graceful window is only real on POSIX.
- An expired lease whose attempt a worker already closed is skipped by the reaper (job stays leased); cancel of a running run relies on the supervisor's 1 s poll or the next gateway call.
- Wall/monotonic clock are used for deadlines and event timestamps; no clock-injection tests beyond the real-deadline test.
- Idempotency-key retention job, DB-outage, disk-full and load tests (M7).

**Smallest next task (M3):** done as slice 1; see the M3 section above.

---

## M1: models, compiler, API shell, schema

All M1 tasks from IMPLEMENTATION.md are implemented, except generating the typed web client, which is M6's task (D-024). Nothing executes an agent: accepted runs stay `queued` until the M2 worker exists.

**Implemented** (`src/agentcrash/`):
- Domain: `domain/canonical.py` (acl-c14n-v1 hashing), `models.py` (strict models, 8-way fault-effect union), `diagnostics.py`, `registry.py`, `ports.py` (Store port + read models), `pagination.py` (opaque keyset cursors).
- Scenarios: `loader.py` (hostile-input-safe YAML *and* JSON), `compiler.py` (registry resolution, ambiguity rejection, normalization, hashing), `manifest.py` (run validation, immutable manifest).
- Storage: `storage/schema.py` (Core tables), `store.py` (`PostgresStore`), `engine.py`; `migrations/versions/0001_initial_schema.py` (hand-written DDL; scenario immutability trigger; one-active-execute-job partial unique index; restrictive FKs); `alembic.ini`, `migrations/env.py`.
- API (`api/`): bearer auth (constant-time), error envelope with server-generated `X-Request-ID`, `GET /health/live|ready`, `GET /api/v1/agents`, `POST|GET /api/v1/scenarios`, `GET /api/v1/scenarios/{id}/versions/{v}`, `POST|GET /api/v1/runs`, `GET /api/v1/runs/{id}`; idempotency, queue cap 100, in-process 10/min rate limit, 1 MiB streamed body cap, keyset pagination. The API imports only `domain` and `scenarios`; the store is injected by `bootstrap.py`.
- CLI: `agentcrash dev init` (token created once, never overwritten; idempotent seeding; refuses an unmigrated database), `agentcrash api` (real app factory), `scenarios validate`, `check` (now also OpenAPI/schema drift).
- Generated: `schemas/domain.schema.json`, `schemas/openapi.json` (drift-checked in `agentcrash check`).

**Commands run and outcomes** (Windows host, PostgreSQL 18.6 from `compose.yaml`):

| Command | Result |
| --- | --- |
| `uv run pytest tests/unit tests/property tests/contract` | 187 passed |
| `uv run pytest tests/integration` (real PostgreSQL) | 33 passed |
| `uv run agentcrash check` | passed: ruff format+lint (incl. migrations), mypy --strict (52 files), schema + OpenAPI drift |
| `uv run alembic upgrade head`, `uv run agentcrash dev init` (twice) | migrated; token created then kept; scenarios created=1 then unchanged=1 |
| Real `agentcrash api` driven with `curl` | 401 without token; run accepted (202, queued); same key replayed (`Idempotent-Replayed: true`); changed body -> 409; list works; `/docs` -> 404 |
| Mutation check: remove the admission lock | `test_queue_cap_holds_under_concurrency` failed 3/3; restored, passes |
| Clean copy of non-ignored files: sync from lock, `uv lock --check`, check, non-DB tests | all passed; no `.env`/`.local` present |

**M1 acceptance**

| Criterion | Evidence |
| --- | --- |
| Invalid probability, unknown field, malicious YAML tag, duplicate rule, invalid target, unknown adapter rejected with stable 422 codes | `tests/contract/test_scenario_compiler.py`, `test_run_and_manifest.py`; over HTTP in `tests/unit/test_api_contract.py` (codes `INVALID_PROBABILITY`, `UNKNOWN_FIELD`, `YAML_TAG_FORBIDDEN`, `DUPLICATE_RULE_ID`, `INVALID_TARGET`, `UNKNOWN_ADAPTER`) |
| Supplied 429 example compiles; equal canonical inputs give equal hashes; changed fixture gives a new manifest hash | `test_scenario_compiler.py`, `test_run_and_manifest.py`, Hypothesis properties; CONTRACTS.md examples are parsed and checked in `test_contract_examples.py` |
| Concurrent identical creation keys yield one run/job | `test_concurrent_identical_creation_keys_yield_one_run_and_one_job` (8 threads, barrier; also contested bodies and distinct keys) |
| Restarting the API retains a queued run | `test_queued_run_survives_an_api_restart` (real processes killed abruptly; run, job and idempotency key survive) |
| Generated schema checks have no drift | domain JSON Schema + OpenAPI drift-checked; typed client deferred to M6 (D-024) |
| No request path executes an agent | no `run_attempts` rows after creation; `api` may not import `agents`/`storage`/`bootstrap` (architecture test) |

**Mistakes caught and fixed this slice:** FK missing from the table definitions (caught by the migration-vs-metadata drift test); `\n` escaped into a real newline in two patch scripts (broke CI YAML and a Python string; caught by validating the workflow and by lint); one file written in cp1252 (caught by ruff, whole repo then scanned for invalid UTF-8); test-harness bugs (duplicate-rule test never duplicated, double `registry` argument, non-ASCII header in the test client).

**Not run / limits**
- GitHub CI (no remote, O-002). The `postgres-integration` job's commands were run by hand, not through Actions.
- Windows only; Linux/macOS untested.
- Previous-release-to-head migration (no previous release exists); downgrade is tested only empty-database round trip.
- DB outage, disk-full and load/latency targets (M7). No coverage threshold measured.
- Rate limiter is per process (D-023). The `POST /scenarios` endpoint is not rate limited.
- Faults needing fixtures (stale snapshots, document injection, conflict patches) still fail with `UNKNOWN_REFERENCE` until M3; the mutation-pointer-vs-tool-schema check is also M3.
- `Idempotency-Key` retention/tombstoning is only exercised by setting `tombstoned_at` in a test; the retention job is M7.

**Smallest next task (M2):** done as slice 1; see the M2 section above.

---

## Repository state

- `git init -b main` was run in this session (the directory was not a repository). One commit exists on `main` (`feat: implement M0 toolchain and M1 control-plane API`, authored by the configured git user, no co-author), made at the user's request on 2026-11-09. Remote `origin` = `https://github.com/aum2606/agentCrash.git` was added at the user's request; nothing has been pushed, so CI still has not run.
- Handoff documents are unchanged except `README.md` (quick start) and `DECISION_LOG.md` (decisions D-010..D-024, O-001/O-002, ledger rows). `SHA256SUMS.txt` describes the original handoff, so it now reports `README.md` as changed; `DECISION_LOG.md` is expected to differ for the same reason. It was not regenerated.
- Branch policy (IMPLEMENTATION.md section 7): `main` stays releasable; short-lived `feat/...`, `fix/...`, `chore/...` branches; squash merge after CI and review. Not enforceable until a host exists (O-002).

## Implemented in M0

- `src/agentcrash` package (uv_build, `py.typed`): `agentcrash version`, `agentcrash api` (uvicorn; refuses non-loopback hosts per ADR-009; default `127.0.0.1:8787`), `agentcrash check`.
- `GET /health/live` returns `200 {"status":"ok"}` with no DB access. `/health/ready`, auth and every `/api/v1` route are **not** implemented.
- `agentcrash check` runs `ruff format --check`, `ruff check` and `mypy --strict` over `src tests` and exits non-zero if any fail. It prints that it is static only. It does not run pytest, integration tests, the architecture check or codegen drift (none exist yet).
- 4 unit smoke tests (`tests/unit/test_smoke.py`): in-process health, CLI version, non-loopback refusal, and a real `agentcrash api` subprocess answering `/health/live` over HTTP on a free port.
- `compose.yaml`: PostgreSQL `18.6-trixie` pinned by digest, named volume, `pg_isready` health check, loopback-only port 15432, password required (no default). `.env.example` has names and an empty password only. `.env`, `.local/`, tokens ignored.
- `.github/workflows/ci.yml` (jobs `static`, `unit`, `postgres`), `pull_request_template.md`, `CODEOWNERS` (no rules; see O-002).
- `docs/toolchain.md` with exact versions, image digest and action SHAs.

## Commands actually run (all on the Windows dev host, 2026-11-09)

| Command | Result |
| --- | --- |
| `uv lock` / `uv sync --frozen --all-groups` | 36 packages resolved and installed on CPython 3.12.13 |
| `uv run agentcrash check` | Passed (also failed once, correctly, when Ruff tried to reformat the handoff Markdown; fixed by scoping, D-012) |
| `uv run pytest` | 4 passed, no warnings (an earlier run had one `httpx` deprecation warning; fixed by `httpx2`) |
| Clean copy of all non-ignored files into a fresh directory, then `uv sync --frozen --all-groups`, `uv lock --check`, import, `agentcrash check`, `pytest tests/unit` | All passed; no `.env` present in the copy |
| `docker compose config` without `.env` | Failed closed with the password-required message |
| `docker compose up -d --wait postgres` | Reached `healthy`; `show server_version` returned 18.6 |
| Create table, insert row, `docker compose down`, `up --wait`, select | Row survived the restart (persistence works) |
| `docker compose down --volumes` | Test stack and volume removed; no `agentcrash` volumes remain |
| Parse `ci.yml` with PyYAML; grep for non-SHA `uses:` | Valid YAML; jobs `static, unit, postgres`; `contents: read`; all actions SHA-pinned |
| `sha256sum -c` on SHA256SUMS (CRLF stripped) | 7 handoff files OK, `README.md` differs (intentional) |

## M0 acceptance status

| Criterion | Status |
| --- | --- |
| Clean checkout installs from lock, imports package | Met locally (clean-copy rehearsal) |
| Runs health endpoint and one meaningful smoke test | Met locally |
| Starts PostgreSQL | Met locally (healthy, persistent) |
| Passes static checks **in CI** | Met: CI green on `origin/main` @ `ad5e8df` (user-reported) |
| No secrets committed | Nothing is committed; `.env` is ignored and absent from the clean copy. No secret scanner was run. |
| Progress/decision records | Done (this file, `docs/toolchain.md`, `DECISION_LOG.md`) |

## Not run / not done

- GitHub Actions workflow execution; Compose-in-CI step (its commands were only run locally, not through `ci.yml`).
- A host-side client connection to PostgreSQL on port 15432 (no DB driver is a dependency yet); only the in-container health check and `psql` were used.
- Linux or macOS runs; everything ran on Windows 11 with Docker Desktop.
- Secret scan, dependency audit, image scan, license inventory (`supply-chain` job from IMPLEMENTATION.md section 6 does not exist; it must not be added as an empty pass).
- `web-e2e`, `mock-regression`, `postgres-integration` (Alembic) jobs: owned by M1-M7. Tracked here instead of creating empty workflows. (The forbidden-import and codegen drift checks now exist; see the M1 section.)
- Node/pnpm frontend lock: no `web/` yet (M6). Python 3.12 is in security-fix-only support; 3.12.16 is available upstream but not installable with uv 0.11.6 (D-010).
- Branch protection, CODEOWNERS rules, license: need O-002 / O-003.

## Next action

Continue M3 with the smallest next task named in the M3 section (payload-mutating effects and fixtures). Still open: CODEOWNERS rules (needs maintainer handle), license (O-003), branch protection.
