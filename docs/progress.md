# Progress ledger

Last updated: 2026-11-09. M0 and M1 are **accepted** (local evidence below plus CI green on `origin/main` @ `ad5e8df`, reported by the user; the run URL was not given to me and I could not view Actions). Current milestone: **M2, in progress** (slice 1 of 6 done).

## M2: durable healthy execution (in progress)

M2 has six tasks in IMPLEMENTATION.md. Only the first is partly done. **Nothing executes an agent yet; accepted runs still stay `queued` unless a test claims them.**

**Done in slice 1** (`storage/queue.py`, `domain/ports.py`; decision D-025):
- `JobQueue.claim_execute`: short transaction, `FOR UPDATE SKIP LOCKED`, new attempt, run fencing token +1, 30 s lease from PostgreSQL `now()`.
- `JobQueue.heartbeat`: needs matching owner and token and an unexpired lease; an expired lease cannot be revived.
- `JobQueue.reap_expired`: closes an expired open attempt as `worker_lost`, invalidates the token, and in the same transaction queues a fresh attempt or, at the cap of 2 attempts, sets the run `error`.
- `lock_fenced_attempt` / `JobQueue.close_attempt`: stale tokens, other attempts' tokens and already-closed attempts raise `StaleFence`.

**Evidence** (Windows host, PostgreSQL 18.6): `tests/integration/test_queue.py`, 16 tests, no sleeps: 8-way claim race gives exactly one attempt; 6 workers get 6 distinct jobs; a locked job does not block another worker; finished runs are never claimed; heartbeat owner/token/expiry rules; reaper requeue, idempotency and cap; stale worker fenced before and after reassignment; concurrent reapers requeue once; reaper racing a fenced close yields exactly one outcome. Mutation checks: removing `SKIP LOCKED` fails the lock test; removing the token comparison fails the stale-worker test. Full run: `agentcrash check` passes, 189 non-DB tests, 49 integration tests.

**Found while testing:** my first reaper overwrote an attempt a worker had just closed (it did not re-check after taking the run lock); fixed and covered by the race test. A test leaked an open connection whose transaction blocked a later `TRUNCATE`, so the run hung until I stopped it; fixed. My first `SKIP LOCKED` test deadlocked itself when it failed; restructured.

**Not done in M2:** worker process/supervisor and child runner, fixture cloning, invoice tools/CAS/receipts, gateway event writer, naive agent and graders, cancellation and deadlines, the M2 acceptance run (healthy run, kill worker after a write, duplicate delivery, cancel before/after write). Known gaps in slice 1:
- An expired lease whose attempt was already closed is skipped by the reaper; the completion slice must mark such jobs `done`.
- `cancel_requested` is not consulted yet.
- `run_attempts.termination` has no database CHECK constraint; the values are enforced in code only.
- No run `error_code` is stored (D-025).
- Linux CI has not run these new tests.

**Smallest next task:** the job completion path: one transaction under `lock_fenced_attempt` that closes the attempt, marks the job `done` and moves the run to `evaluating`, then a minimal worker loop (claim, heartbeat thread, no agent yet) with a test that kills the worker process mid-lease and checks the reaper recovers it.

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
