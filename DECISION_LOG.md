# AgentCrash Lab — decision and progress ledger

Baseline: 2026-11-09. No implementation work is complete at handoff. Accepted-for-implementation defaults are in [ADRS.md](ADRS.md); this log separates them from assumptions and unresolved decisions.

## Initial decision log

| ID | Date | Decision | Status | Reason / evidence | Owner / revisit |
| --- | --- | --- | --- | --- | --- |
| D-001 | 2026-11-09 | Build local synthetic invoice lab first | Accepted default | Original concept plus bounded, measurable MVP | Maintainer; after M7 |
| D-002 | 2026-11-09 | Modular Python API/worker, PostgreSQL jobs | Accepted default | ADR-001/003; atomic state/evidence | Backend owner; measured contention |
| D-003 | 2026-11-09 | Deterministic reference agents before live model adapter | Accepted default | ADR-002; offline reproducibility | Maintainer; Phase 2 |
| D-004 | 2026-11-09 | Separate replay from stochastic rerun | Accepted default | ADR-006; no unsupported determinism claims | Execution owner; model cassettes |
| D-005 | 2026-11-09 | Evidence ledger independent of OTel mapping | Accepted default | ADR-008; upstream GenAI conventions moved | Observability owner; mapping revision |
| D-006 | 2026-11-09 | No live model/provider selected or paid calls authorized by plan | Deferred | M7 does not require provider access | Maintainer; Phase 2 approval |
| D-007 | 2026-11-09 | Baseline version families only; resolve exact locks at M0 | Accepted; locks recorded | ADR-011; exact pins and install/run evidence in [docs/toolchain.md](docs/toolchain.md); frontend (Node/pnpm) lock deferred to M6 | Implementer; refresh on security update |
| D-008 | 2026-11-09 | Post-commit timeout may pass state oracle despite naive reported error | Accepted default | Outcome grading must not manufacture demo failures | Evaluation owner; M3 fixtures |
| D-009 | 2026-11-09 | No public deployment, arbitrary code upload or production-tool faults in MVP | Accepted boundary | ADR-009 | Maintainer; new security design |
| D-010 | 2026-11-09 | Pin Python 3.12.13, not the newest 3.12.16 | Accepted (narrow deviation) | uv 0.11.6 has no 3.12.16 build ("No download found"); 3.12.13 is the newest it can install, and the patch gap is within the same security-fix-only 3.12 line. Stack family unchanged | Implementer; when uv is upgraded or a 3.12 security fix matters |
| D-011 | 2026-11-09 | Pin CI to uv 0.11.6 and `uv_build<0.12` although uv 0.13.7 exists | Accepted | Tested == pinned; the setup-uv default would float to latest and run untested tooling | Implementer; deliberate uv bump PR |
| D-012 | 2026-11-09 | `httpx2` (not `httpx`) as the test HTTP client; Ruff scoped to `src tests` | Accepted | Starlette 1.10 emits a deprecation warning for `httpx` and asks for `httpx2`; Ruff 0.17 reformats Markdown code fences, which would rewrite the normative handoff docs | Implementer; revisit if Starlette/Ruff behavior changes |
| D-013 | 2026-11-09 | Compose PostgreSQL host port defaults to 15432 (loopback), overridable via `POSTGRES_HOST_PORT` | Accepted | 5432 is commonly taken; 54320 failed to bind on the Windows dev host (excluded port range) | Implementer |
| D-014 | 2026-11-09 | `.github/CODEOWNERS` has no active rules; API default port 8787 | Accepted (tracked) | No maintainer identity or repository host is known (O-002); inventing handles would be wrong. Rules must be added before branch protection | Maintainer; O-002 |
| D-015 | 2026-11-09 | Which tools each fault effect may target is a declarative table (`environments/invoice/catalog.py`): `timeout_after_commit` only on `ledger.post_invoice`; `scripted_conflict` only on `ledger.post_invoice`; `document_injection` only on `document.get`; `schema_mutation`/`stale_read` on read tools; `vendor.update_bank_account` accepts no fault. Rules must target a tool the policy allows. Fault stages follow ENGINEERING_PLAN section 4 (`scripted_conflict` = before dispatch) | Accepted (interpretation) | CONTRACTS only fixes `timeout_after_commit` = mutating tools and `stale_read` = read tools; the rest is inferred from the plan's stage table. Fail-closed and data-only, so widening later is a one-line change | Execution owner; M3 when the fault engine consumes it |
| D-016 | 2026-11-09 | Compiler normalizes but never relaxes: it sorts rules/graders/tools (so order cannot change a hash), adds missing integrity/safety graders and deployment-forbidden tools, and **rejects** (`REQUIRED_GRADER_RELAXED`) a scenario that sets one of those graders `required:false`. Faults whose fixtures do not exist (stale snapshots, injection documents, conflict patches) fail with `UNKNOWN_REFERENCE` | Accepted (interpretation) | CONTRACTS: "a submitted scenario cannot remove them". Rejecting is explicit rather than silently coercing; fixtures arrive in M3 | Evaluation owner; M3/M5 |
| D-017 | 2026-11-09 | Budget caps are the MVP constants in `Budgets` (requests may lower, not raise); raising caps via local deployment config is not yet implemented. Registry lists `invoice-naive`/`invoice-resilient` 1.0.0 with empty config before their adapters exist | Accepted (interim) | CONTRACTS: "raising deployment caps needs local configuration". Runs for these agents validate but cannot execute until M2/M3 | Backend owner; M2 |
| D-018 | 2026-11-09 | Diagnostic paths use the real document pointer (e.g. `/faults/0/effect/type`); the CONTRACTS error example shows `/faults/0/type`, which does not match the nested schema, so it is treated as illustrative | Accepted | Paths must locate the offending field in the submitted document | Contract owner; if clients depend on the example |
| D-019 | 2026-11-09 | M1 uses synchronous SQLAlchemy Core + psycopg 3; FastAPI endpoints are plain `def` (threadpool) | Accepted (maintainer approved recommendation) | The API only validates and stores; nothing on its path needs async. The contract's async ports (gateway, event writer) are M2 and may use their own connection handling | Backend owner; M2 |
| D-020 | 2026-11-09 | Scenario-version immutability is enforced by a PostgreSQL trigger (UPDATE/DELETE raise) as well as by code; integration tests fail (never skip) when PostgreSQL is unreachable and use throwaway databases | Accepted (maintainer approved recommendation) | CONTRACTS: "immutable". A missing database must not look like a pass | Backend owner |
| D-021 | 2026-11-09 | POST bodies are read raw (1 MiB streamed cap) and parsed by the same hostile-input rules for JSON and YAML (scenarios accept `application/json` or `application/yaml`; runs accept JSON). Optional query parameters and `Idempotency-Key` are read from the request and documented via `openapi_extra`; request schemas are injected into `components` | Accepted | Keeps `SCENARIO_/RUN_VALIDATION_FAILED` diagnostics under our control instead of framework 422 shapes; declaring alias/union parameters made Pydantic 2.14 emit `UnsupportedFieldAttributeWarning` | API owner; if FastAPI fixes the warning |
| D-022 | 2026-11-09 | Run creation: idempotency claim -> advisory-lock admission check (queue cap 100) -> run + execute job, all in one transaction. The request hash is the canonical hash of the *validated* RunCreate (spelling out defaults is the same request). A replay returns 202 with `Idempotent-Replayed: true`; re-posting identical scenario content returns 200; a rejected admission rolls the claim back so the key stays usable. The stored scenario is re-compiled against today's registry at run creation (`SCENARIO_NO_LONGER_VALID` on drift) | Accepted | CONTRACTS section 3; the cap-under-concurrency test fails when the lock is removed (mutation-checked) | Backend owner |
| D-023 | 2026-11-09 | Additive schema/response choices: `runs.labels` JSONB column (labels are not in the manifest); `experiments` table created now as the `runs.experiment_id` FK target; `RunRecord` is a superset of the contract example (adds `scenario`, `agent`, `seed`, `labels`); keyset order is newest first; local rate limiter (10/min) is in-process | Accepted | CONTRACTS lists no labels column and says clients may ignore added fields. Rate limiter is correct only for one API process (documented shortcut) | Backend owner; before multi-replica use |
| D-024 | 2026-11-09 | Interactive docs and `/openapi.json` are not served; the spec is the committed `schemas/openapi.json` with a drift check. The typed client is generated in M6 (its own task), so "client drift" is verified there | Accepted | `/docs` would load assets from a CDN (ADR-009 local-only). IMPLEMENTATION M1 mentions the client, M6 builds it | API owner; M6 |
| D-025 | 2026-11-09 | Queue mechanics (M2 slice 1), `storage/queue.py`: a claim leases the oldest `queued` execute job (`FOR UPDATE SKIP LOCKED`), bumps `runs.fencing_token`, creates the next attempt and sets the run `running`. Lock order is always job -> run -> attempt; every fenced write calls `lock_fenced_attempt` inside its own transaction. The reaper closes an expired attempt as `worker_lost` only if it is still open after taking the run lock, bumps the token, marks the old job `error`, and either inserts a new job (`dedupe_key execute:<run>:<n>`) or, at the 2-attempt cap, sets the run `error`. Heartbeat refuses an already-expired lease. A recovering run stays `running`, not `queued`. No new column: `INFRA_WORKER_LOST` is not stored on the run yet | Accepted | Follows ENGINEERING_PLAN section 3 and ADR-004. Not adding an `error_code` column avoids an unrequested schema change; it will be needed when the API must show the reason (decide in M2 completion/cancel slice) | M2 later slices |
| D-026 | 2026-11-10 | The invoice fixture hash covers the state file and the benign document text (`fixtures/invoice/v1/documents/benign.txt`), so editing either changes every manifest that uses it. Additive: `invoice-429`'s fixture hash value changes once | Accepted | The document is part of what the agent sees; leaving it out of the hash would let it change silently | M3 adds injection fixtures to the same hash |
| D-027 | 2026-11-10 | M2 execution choices: the agent runs in a child process spawned with `python -I -m agentcrash.execution.child`, an allowlisted environment and the capability on stdin; gateway transport is stdlib `http.server` on 127.0.0.1 (no extra dependency); a child that exits without a result takes the infrastructure path (`worker_lost`, replacement attempt); base tool latency is 0 logical ms; `runs.error_code` added (nullable, additive; also exposed on the run response); `FAULTS_NOT_IMPLEMENTED` / `ADAPTER_NOT_IMPLEMENTED` / `FIXTURE_CHANGED` end a run in `error` instead of running it | Accepted | Keeps the agent isolated from storage and secrets, adds no service or dependency, and never reports an unfaithful run as a pass | Revisit when M3 adds faults or if remote workers are needed |
| D-028 | 2026-11-11 | Fault engine v1 (M3 slice 1), pure `faults/matcher.py`: sample = first 8 bytes (big-endian) of SHA-256 over the canonical JSON array `[seed, scenario_hash, actor_id, tool, tool_ordinal, rule_id]` (`seed` is the manifest's decimal **string**), compared with the probability as exact fractions (no floats). Only eligible rules (tool, actor, ordinal all match) are decided and recorded; a rule is `applied`, `not_sampled` or `exhausted`, and only `applied` consumes `max_applications`. Applied counts are derived from the attempt's own `fault.decided` events (`applied: true`), so a retransmitted call never double counts. Order: stage, priority, rule ID. Policy denial happens before fault matching, so a denied call decides nothing. The `fault.decided` payload is `{rule_id, effect, stage, tool, tool_ordinal, sample_u64 (string), probability, decision, applied, applications_before, applications_after}` | Accepted | Follows ENGINEERING_PLAN section 4; a golden sample value in `tests/unit/test_fault_matcher.py` makes any serialization change a deliberate `fault_engine_version` bump | `FAULT_ENGINE_VERSION` stays "1" until released; bump if the hash input or event payload changes |
| D-029 | 2026-11-11 | Gateway fault stages implemented: `delay` and `timeout_*` advance logical time through `clock.advanced` events (`cause: delay` or `timeout`); `http_error` returns the status without dispatching (body `{error:{code: RATE_LIMITED or UNAVAILABLE, injected: true, retry_after_ms?}}`); timeouts raise `ToolTimeout` after the transaction commits and cross the loopback as `{kind:"transport_error"}` (CONTRACTS section 2). The stored call outcome is the transport error, so a retransmitted call ID raises the same timeout and allocates no ordinal; the call's terminal event is `tool.transport_failed` (with a `committed` flag), never also `tool.completed`. A `delay` that cannot fit the logical budget aborts the call before the operation (`BUDGET_EXCEEDED`, nothing persisted for that call); a timeout may overrun the budget, after which the next call is refused and the budget grader fails the attempt. `timeout_after_commit` suppresses the response whether or not the call committed anything (`committed: false` is recorded when it did not). The supervisor still refuses scenarios whose effects are not implemented (`schema_mutation`, `stale_read`, `document_injection`, `scripted_conflict`) with `FAULTS_NOT_IMPLEMENTED`. Five new reviewed scenarios: `invoice-503`, `invoice-timeout-before`, `invoice-timeout-after`, `invoice-latency`, `invoice-schema-change` (the last is still refused) | Accepted | Keeps evidence authoritative and the agent unable to tell a simulated timeout from a real one; no contract text changed | Revisit when payload-mutating effects land (next M3 slice) |
| D-030 | 2026-11-11 | `schema_mutation` (M3 slice 2): `domain/pointer.py` resolves object-key-only JSON Pointers (no arrays, no root, rename onto an existing key refused). `ToolSpec.body_shape` declares each mutable tool's successful body as nested keys; the compiler rejects a pointer that names nothing (`POINTER_NOT_FOUND`) or a rename onto an existing key (`RENAME_TARGET_EXISTS`) so a compiled rule can never be a runtime no-op on a success. At run time the mutation applies to the already-redacted body of a 200 response only; on any other status it is recorded as `applied:false, reason:non_success_status`. `tool.completed` gains optional `original_body_ref` and `mutation:{rule_id,operation,pointer,applied}` (additive, absent when no rule applied). The naive agent now posts the purchase order's `approved_amount_minor`/`currency` (it trusts that schema), which is what makes both the renamed-field and the future stale-PO case fail for the designated reason; healthy-path arguments are unchanged | Accepted | CONTRACTS section 5 ("Missing pointer fails scenario compilation") and section 6 (naive "expects original schema"); no normative text changed | Revisit if tools gain nested arrays |

## Assumptions to validate

| ID | Assumption | Impact if false | Validation / action |
| --- | --- | --- | --- |
| A-001 | Developer can run Linux containers and PostgreSQL locally | Setup path changes | M0 quick-start rehearsal; document Docker Desktop requirements |
| A-002 | Four simultaneous mock attempts is sufficient initially | Queue/DB design needs measurement | M7 workload before broker adoption |
| A-003 | Synthetic invoice workflow is acceptable as first domain | Fixture and grader scope changes | Maintainer can revise before M2; preserve generic core |
| A-004 | Local runtime storage is acceptable | Artifact backend changes | Confirm before distributed deployment |
| A-005 | Selected version families remain supported and compatible when coding starts | Toolchain defaults need revision | Verify official notices at M0 and record tested pins |

## Open decisions and gates

| ID | Question | Default / current state | Resolve by | Blocking? |
| --- | --- | --- | --- | --- |
| O-001 | Exact package patches, lockfiles and OCI/action digests? | Resolved for M0 (Python, PostgreSQL image, actions); Node/pnpm lock deferred to M6 | M0 | Resolved |
| O-002 | Repository host/org and maintainers? | Partly resolved 2026-11-09: user supplied the host, `origin` = `https://github.com/aum2606/agentCrash.git` (added locally; nothing pushed, no commit). Maintainers still unnamed, so CODEOWNERS has no rules; CI has not run | Before protected-main/release setup | Blocks the "passes in CI" part of M0 acceptance, not local coding |
| O-003 | Project license? | No license assumed | Before public release | Blocks external publication, not private coding |
| O-004 | Provider, model, secret method and live budget? | None configured | Phase 2 | Blocks paid/live trials only |
| O-005 | Live regression tolerance and trial sample size? | Reports informational; proposed 20 pairs/scenario | Phase 2 experiment ADR | Blocks statistical release gate only |
| O-006 | Team auth and hostile workload isolation? | Excluded from MVP | Before remote/multi-user launch | Blocks that expansion |

Do not interrupt M0 for questions that only affect a later phase. Never resolve a paid call, publication, destructive migration or public hosting decision through elapsed time or silence.

## Milestone ledger

| Milestone | Status | Evidence | Next step |
| --- | --- | --- | --- |
| M0 Toolchain/repository | **Accepted 2026-11-09**: local evidence plus CI green on `origin/main` @ `ad5e8df` (user-reported; run URL not recorded) | [docs/progress.md](docs/progress.md), [docs/toolchain.md](docs/toolchain.md) | Done |
| M1 Contracts/compiler/API | **Accepted 2026-11-09**: CI green on `origin/main` @ `ad5e8df` (user-reported; run URL not recorded). All M1 tasks done except the typed client (D-024, M6). 222 tests incl. 33 on real PostgreSQL | [docs/progress.md](docs/progress.md) | M2 |
| M2 Durable execution | **Accepted 2026-11-11**: CI passed on `origin/main` @ `821db83` (user-reported; run URL not recorded). All six tasks for the healthy path; 241 non-DB + 91 PostgreSQL tests passed locally | [docs/progress.md](docs/progress.md) | M3 |
| M3 Faults/agents | **In progress (slice 2)**: pure matcher; `http_error`, both timeouts, `delay` and `schema_mutation` applied; 5 more suite scenarios; not accepted | [docs/progress.md](docs/progress.md) | Stale/injection/conflict fixtures, resilient agent, loop detection |
| M4 Replay/bundles | Not started | None | After M3 |
| M5 Experiments/gates | Not started | None | After M4 |
| M6 Web review | Not started | None | After M5; scaffolding may begin after M1 |
| M7 Release hardening | Not started | None | After M6 |

Create `docs/progress.md` at M0 with current branch/commit, milestone task, checks actually run, known gaps and next action. Keep session history there; this file records durable decisions and milestone acceptance.

## Decision-entry template

```markdown
### D-NNN — short decision title
Date / owner:
Status: proposed | accepted | superseded | deferred
Problem and constraints:
Decision:
Alternatives and why not chosen:
Affected contracts / code / milestone:
Evidence (tests, benchmark, documentation):
Risks / compatibility / rollback:
Revisit date or trigger:
ADR link when architectural:
```

## Milestone acceptance entry template

```markdown
### Mx acceptance — date
Source commit and working-tree state:
Completed task IDs / behavior:
Acceptance checks and exact commands:
Observed results and sanitized evidence paths:
Unrun/failed checks and reasons:
Decision / ADR updates:
Known limitations:
Maintainer review (when required):
Next task:
```

## Verification of external conventions

Sources were opened on 2026-11-09. Only the listed conventions were verified; no dependency installation, application test or benchmark was performed for this planning package. Paraphrases and design implications below are intentionally narrow.

| Source | Verified point | Plan consequence |
| --- | --- | --- |
| [Claude Code project memory](https://code.claude.com/docs/en/memory) | Root CLAUDE.md supports project instructions; concise files are recommended | Keep always-read file small and reference detailed documents |
| [Claude Code best practices](https://code.claude.com/docs/en/best-practices) | Clear validation and controlled context support coding workflows | Explicit acceptance checks and session progress |
| [Anthropic agent evaluations](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | Outcome-based grading, deterministic graders where possible, transcript inspection and judge calibration | Trusted end-state graders first; trace evidence and unknown results |
| [OTel relocation notice](https://opentelemetry.io/docs/specs/semconv/gen-ai/) and [GenAI repository](https://github.com/open-telemetry/semantic-conventions-genai) | GenAI conventions moved to a dedicated repository | Pin mappings independently of stored evidence format |
| [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html) | SKIP LOCKED supports queue-like consumers with consistency caveats | Short claim transactions; never use skipped rows as a correctness oracle |
| [uv GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/) | Lock-aware CI installation workflow | Frozen installs and runtime/toolchain pins |
| [FastAPI schema examples](https://fastapi.tiangolo.com/tutorial/schema-extra-example/) | Pydantic/FastAPI support JSON Schema/OpenAPI examples | Generate and test schemas/examples from models |
| [GitHub secure use](https://docs.github.com/en/actions/reference/security/secure-use) | Least-privilege credentials and secure workflow inputs/actions | Read-only PR jobs, pinned actions, no untrusted-code secrets |
| [Docker security](https://docs.docker.com/engine/security/) | Daemon/configuration and kernel isolation affect security | Nonroot/no socket/private networks; no claim of hostile-code sandboxing |

All numerical budgets, milestones, statistical policies and stack choices in this handoff are project design decisions. The sources do not certify those choices or their future performance.
