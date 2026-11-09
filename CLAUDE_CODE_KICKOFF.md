# Claude Code kickoff

Copy all handoff files into the intended repository root, open Claude Code there, and paste the prompt below. It authorizes local M0 implementation; it does not authorize live model spending, external publication or destructive repository operations. Subsequent milestones can be assigned after reviewing M0 evidence.

```text
Implement AgentCrash Lab from the engineering handoff in this repository.

Read CLAUDE.md, ENGINEERING_PLAN.md, CONTRACTS.md, IMPLEMENTATION.md,
ADRS.md and DECISION_LOG.md, plus any existing repository instructions.
Inspect the current files, Git status/history and progress before editing.
Preserve existing work. These files describe intended behavior; do not assume
the application, commands, dependencies or tests already exist.

Your assignment for this session is M0 in IMPLEMENTATION.md. Implement it,
run its acceptance checks, fix failures, and leave a working minimal package.
Do not stop at another plan or scaffold with false-success placeholders.
Do not start M1–M7 in this session; keep the first change reviewable.

Choose exact supported compatible patches for the documented baseline
families, verify relevant official support/security documentation, lock them,
and record the tested versions, action SHAs and image digests in
docs/toolchain.md. If an accepted baseline cannot be used, explain evidence
and record a narrow decision instead of silently replacing the stack.

Required M0 outputs:
- An installable Python package under src/agentcrash with the minimal CLI,
  local API /health/live and a meaningful smoke test.
- pyproject.toml, uv.lock, runtime pin, Ruff/mypy/pytest configuration and
  a working check command for the checks currently implemented.
- PostgreSQL Compose service with persistent local storage, health check,
  safe .env.example, ignored secrets/runtime state and setup instructions.
- Minimal real CI with pinned external actions and read-only PR permissions,
  a PR template and a CODEOWNERS file using actual maintainers if known;
  do not invent identities. Track unknown host/owner details explicitly.
- docs/toolchain.md and docs/progress.md with actual commands/results,
  known limitations and the next M1 task. Update DECISION_LOG.md as needed.

The full design is a modular Python API/worker system with PostgreSQL jobs,
fenced attempts, transactional tool receipts/events, deterministic faults,
trusted state graders and strict recorded-response replay. M0 must leave
room for these boundaries without implementing later features prematurely.

No model API key, live provider request, real financial write, public endpoint,
arbitrary uploaded-code execution, Redis, Kubernetes or extra microservice.
Do not delete/reset existing work, modify remotes, push, merge, publish or
deploy. Do not create a Git commit unless I have authorized commits.
Use reasonable local implementation judgment without repeatedly asking
permission. Ask only for missing decisions that block this assigned scope;
continue independent work while waiting.

At completion report:
1. What works and which M0 acceptance criteria are satisfied.
2. Commands actually run and their outcomes; identify anything unrun/blocked.
3. Decisions or baseline changes and remaining limitations.
4. Files changed and the smallest next task for M1.

Never claim that the remaining product has been implemented or tested.
```

## Follow-on milestone prompt

```text
Continue AgentCrash Lab with the next uncompleted milestone in
IMPLEMENTATION.md. Read CLAUDE.md, docs/progress.md, DECISION_LOG.md and the
relevant contracts/ADRs. Verify the prior milestone's acceptance evidence.
Implement the next coherent slice, run meaningful focused checks and that
milestone's required acceptance checks, update progress/decisions, and report
actual behavior and remaining gaps. Preserve the local mock-first security
and cost boundaries. Do not silently change normative contracts or mark a
milestone complete without evidence. No push/publication/deployment or paid
model calls unless separately authorized.
```
