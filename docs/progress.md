# Progress ledger

Last updated: 2026-11-10. M0 and M1 are **accepted** (local evidence below plus CI green on `origin/main` @ `ad5e8df`, reported by the user; the run URL was not given to me and I could not view Actions). Current milestone: **M2 implemented locally, not accepted** (CI has not run it).

## M2: durable healthy execution (implemented locally; not accepted)

All six M2 tasks are implemented for the healthy path with the naive agent. CI has not run this code; M2 is accepted only after it does. Faults and the resilient agent are M3: runs needing them end in `error` (`FAULTS_NOT_IMPLEMENTED`, `ADAPTER_NOT_IMPLEMENTED`), never as a fake pass.

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

**Smallest next task (M3):** the pure fault matcher (`faults/`: stable SHA-256 sampling, stage ordering, ambiguity checks) with property tests, then wire `fault.decided` into the gateway.

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

Continue M2 with the smallest next task named in the M2 section. Still open: CODEOWNERS rules (needs maintainer handle), license (O-003), branch protection.
