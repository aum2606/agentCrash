# AgentCrash Lab — architecture decision records

Date: 2026-11-09. Status `accepted-for-implementation` means this handoff selects a default; it is not a claim of production validation. Each record includes a revisit trigger. Preserve records when superseded and link the replacement. Future substantial records may live in `docs/adr/`; keep this index current.

## ADR-001 — modular monolith, independent API and worker processes

**Status:** accepted-for-implementation.

**Context:** The difficult problem is reliable execution/evidence semantics, while workload is local and bounded. Service boundaries too early would multiply failure modes.

**Decision:** One typed Python package. Deploy API and worker separately; run agent adapter in a supervised child. Tool gateway remains a module behind a private interface owned by the supervisor. PostgreSQL is the single transactional authority.

**Alternatives:** Separate fault-proxy/trace/evaluation microservices; single API process with background tasks. Microservices add protocol/operational overhead; request-process background tasks cannot provide durable execution ownership.

**Consequences:** Shared releases and simpler transactions; process-level isolation is limited. Trusted adapters only. Unit boundaries must remain explicit to avoid import coupling.

**Revisit:** Independent scaling/security needs, remote workers, or measured API/worker release contention.

## ADR-002 — deterministic mock-first product

**Status:** accepted-for-implementation.

**Context:** Paid, stochastic model calls make it hard to isolate harness bugs and reproduce the core demo.

**Decision:** Ship deterministic Python reference agents and synthetic invoice tools first. Clearly label their capabilities as scripted reference behavior. No provider credentials are required through M7. Live model evaluation is opt-in Phase 2.

**Alternatives:** Build around one model/framework SDK immediately; build only a generic proxy without a domain. The former couples validation to external variability; the latter lacks a meaningful end-state oracle.

**Consequences:** Reproducible CI and a fast path to a working demo; MVP does not establish named-model resilience. Synthetic invoice tasks intentionally remain narrow.

**Revisit:** M7 completion and explicit provider/model/cost decisions for Phase 2.

## ADR-003 — PostgreSQL queue, state and authoritative event ledger

**Status:** accepted-for-implementation.

**Context:** Runs, idempotency receipts, state mutations and evidence need strong consistency. The initial concurrency cap is 4.

**Decision:** PostgreSQL jobs with short SKIP LOCKED claims, expiring leases, reaper, fencing tokens and transactionally recorded state/events. No Redis/Celery/Kafka in MVP. At-least-once delivery plus idempotency, not an exactly-once execution claim.

**Alternatives:** Celery/Redis adds coordination and duplicate delivery complexity; Kafka is unnecessary for this workload; in-memory queues lose accepted work on restart.

**Consequences:** Less infrastructure, but lease/reaper code must be carefully tested. DB availability gates execution. Large evidence volumes cannot grow without limit.

**Revisit:** Sustained queue pickup >2 seconds with free worker capacity, DB contention attributable to job claims, or remote/large-scale worker requirements. Benchmark before replacing.

## ADR-004 — fresh attempt after worker loss

**Status:** accepted-for-implementation.

**Context:** A worker may die after a state commit but before agent delivery. Arbitrary agent continuation cannot safely be reconstructed from a partial process state.

**Decision:** Mark lost attempt inconclusive; fence it; create an isolated fresh attempt, maximum 2 total by default. Persist all attempts and cost. No implicit checkpoint resume. Task failure does not trigger infrastructure restart.

**Alternatives:** Resume the agent midstream; restart invisibly using the same environment; declare every worker death a task failure. Each confuses infrastructure and agent behavior or duplicates effects.

**Consequences:** Work may be repeated; transparent attempt histories and shared run spending cap are required. Not suitable for real irreversible external writes without a new protocol.

**Revisit:** Durable agent/model session checkpoints or real-tool support with externally reconciled idempotency.

## ADR-005 — declarative fault rules and stable per-rule sampling

**Status:** accepted-for-implementation.

**Context:** Reproducible chaos must not depend on unrelated rule order or thread scheduling, and scenario files must not execute code.

**Decision:** Strict versioned YAML/JSON, allowlisted effects and tool names, exact ordinal matching, SHA-256-derived samples, explicit stage order, compile-time rejection of ambiguous effects. Logical clock for mock delays. Sequential calls per attempt.

**Alternatives:** Arbitrary Python callbacks, global seeded RNG, real sleeps everywhere. Callbacks create execution/security risk; global draws couple behavior to incidental order; sleeps slow tests and add nondeterminism.

**Consequences:** Less expressive than a full chaos DSL, but testable semantics. Real transport and concurrent-agent scheduling require separate contracts.

**Revisit:** A reviewed use case cannot be represented safely, or Phase 3 concurrent tools/agents start.

## ADR-006 — distinguish playback, strict replay and rerun

**Status:** accepted-for-implementation.

**Context:** A seed does not guarantee reproducible live model outputs. Replaying stored outputs is different from reexecuting an environment.

**Decision:** Playback renders evidence; strict replay reruns a compatible deterministic adapter against recorded responses, verifies the complete observable sequence, and performs no real tool writes; rerun executes a new attempt/environment. Strict live-agent replay requires recorded model boundaries, deferred to Phase 2.

**Alternatives:** Call any same-seed run a replay; reconstruct final state by mutating a live environment. Both overstate guarantees and can produce side effects.

**Consequences:** Honest UI/API terminology and precise divergence diagnostics; strict replay does not independently revalidate the tool environment implementation. Rerun determinism tests cover that separately.

**Revisit:** Model cassette implementation or parallel scheduler adds a defined new replay capability.

## ADR-007 — trusted state graders before model judges

**Status:** accepted-for-implementation.

**Context:** A natural-language success claim is unreliable. Safety findings and incomplete evidence need stable interpretation.

**Decision:** Deterministic state/receipt/trace graders decide MVP verdicts. Integrity unknown makes the result inconclusive; attempted unsafe actions remain findings even if blocked. Model judges later start informational and require calibration.

**Alternatives:** LLM judge for every run; grade an exact tool sequence. The former adds variance/cost and prompt-injection risk; the latter rejects valid alternate solutions.

**Consequences:** Domain-specific oracles require deliberate fixture design. Temporal predicates still inspect traces, but task completion focuses on end state. Report denominators include operational dropouts explicitly.

**Revisit:** A task requires semantic judgment that deterministic predicates cannot measure; define reviewed labels, rubric and calibration evidence first.

## ADR-008 — durable event evidence separate from OpenTelemetry

**Status:** accepted-for-implementation.

**Context:** Telemetry may be sampled, dropped or renamed and should not be the sole source of grading/replay evidence.

**Decision:** Append-only bounded PostgreSQL events with deterministic projection/hash chain and closure metadata; payload-free optional OTel diagnostics. Keep GenAI convention mappings in a pinned adapter, separate from stored event versions.

**Alternatives:** Use only a tracing vendor/OTel collector; store raw provider transcripts by default. The first can lose authoritative evidence; the second leaks data and grows storage unpredictably.

**Consequences:** More event-schema ownership, but replay/grade integrity survives exporter failure. Hash chains do not defend against a DB administrator. Live redaction may prevent full replay and must be declared.

**Revisit:** Storage volume exceeds the envelope or an evidence signing/attestation requirement appears.

## ADR-009 — local trusted single-user security envelope

**Status:** accepted-for-implementation.

**Context:** Testing prompt injection produces hostile text. Executing arbitrary hostile code or exposing a public test service is a much larger security problem.

**Decision:** Loopback, local bearer token, no CORS wildcard, allowlisted shipped adapters/graders, synthetic tools with no outbound URLs, private runner interface, nonroot containers and no Docker socket. Treat document text as data. Python protocol/process boundaries are not a sandbox for malicious code.

**Alternatives:** Public multi-tenant SaaS immediately; arbitrary adapter upload; trust an instruction in CLAUDE.md as enforcement. None supplies adequate isolation on its own.

**Consequences:** Narrow adoption path; team/remote use and untrusted execution are intentionally excluded. Paid live experiments need explicit network/secret/cost controls.

**Revisit:** Any request to bind publicly, accept user code, share a deployment across users or access production tools.

## ADR-010 — static React client, FastAPI source contracts

**Status:** accepted-for-implementation.

**Context:** Product needs interactive trace inspection, not server rendering, content SEO or a second backend.

**Decision:** React/TypeScript/Vite with generated OpenAPI client and polling; serve built assets under API origin. FastAPI/Pydantic models generate contracts. No Next.js/SSR, WebSocket or independent auth backend in MVP.

**Alternatives:** Next.js full stack; backend-rendered templates only. Next.js adds another runtime and auth surface; templates complicate stateful trace comparison.

**Consequences:** Smaller deployment; richer features may require polling changes later. Generated client drift is a CI gate.

**Revisit:** Streaming trace volume makes polling inadequate, or a concrete SSR requirement appears.

## ADR-011 — supported baselines with exact implementation-time locks

**Status:** accepted-for-implementation.

**Context:** A plan can select compatible version families but cannot honestly supply untested patches or future model pricing.

**Decision:** Python 3.12/PostgreSQL 18/Node 24 baseline families; M0 verifies support and compatibility, pins patches/tooling/images, commits locks and records actual evidence. Live provider/model is not selected until Phase 2. Use full action SHAs and immutable release image digests.

**Alternatives:** Floating `latest` or invented patch numbers; freeze all dependencies forever. Floating versions break reproducibility; indefinite freezes accumulate vulnerabilities.

**Consequences:** M0 includes dependency selection and compatibility work. Automated updates are reviewed with regression tests and a refreshed toolchain manifest.

**Revisit:** Vendor support ends, a relevant security update ships, or a required library no longer supports the baseline.

## ADR-012 — local content-addressed bundles with reference-aware retention

**Status:** accepted-for-implementation.

**Context:** MVP needs portable evidence without deploying object storage. Partial writes and premature cleanup can destroy replayability.

**Decision:** Authoritative bounded execution data in DB; content-addressed local exports use temp/fsync/rename, then metadata publication. Store artifact references and explicit pins. Retain 30 days by default, sweep only unreferenced artifacts after grace. Back up DB and files together after draining.

**Alternatives:** Object storage from day one; storing artifacts by arbitrary filesystem path; deleting on mtime alone. These add setup or fail reference/integrity needs.

**Consequences:** Single-host topology; capacity monitoring is necessary. Object storage can replace the backend behind the artifact port later.

**Revisit:** Multi-host workers, larger retained payloads or single-host capacity limits.

## Future ADR template

```markdown
## ADR-NNN — concrete decision title
Date:
Status: proposed | accepted-for-implementation | accepted-validated | superseded
Owner:
Context and constraints:
Decision:
Alternatives considered:
Consequences and risks:
Validation evidence:
Revisit trigger:
Supersedes / superseded by:
Related milestone / contract / decision log:
```
