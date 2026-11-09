# AgentCrash Lab — implementation handoff

Planning baseline: 2026-11-09. Status: ready for implementation; no application code has been implemented or tested by this handoff.

AgentCrash Lab tests how agents behave when tools and retrieved data fail. The first release is a local, single-user laboratory with a synthetic invoice-processing environment, repeatable faults, trace inspection, deterministic graders, and CI regression gates.

## Use this package

1. Copy this directory's contents into the root of a new AgentCrash Lab repository. If using an existing repository, merge files deliberately; preserve its history and instructions.
2. Read [ENGINEERING_PLAN.md](ENGINEERING_PLAN.md), [CONTRACTS.md](CONTRACTS.md), and [IMPLEMENTATION.md](IMPLEMENTATION.md).
3. Keep [CLAUDE.md](CLAUDE.md) at the repository root. It is the concise project instruction file; the longer documents are read on demand.
4. Paste the fenced prompt in [CLAUDE_CODE_KICKOFF.md](CLAUDE_CODE_KICKOFF.md) into Claude Code in that repository.
5. Implement milestones in order. Record evidence and decisions in [DECISION_LOG.md](DECISION_LOG.md). Architectural changes go through [ADRS.md](ADRS.md).

## Package map

| File | Purpose |
| --- | --- |
| [ENGINEERING_PLAN.md](ENGINEERING_PLAN.md) | Scope, architecture, execution, injection, replay, evaluation, operations, security |
| [CONTRACTS.md](CONTRACTS.md) | Normative component interfaces, HTTP contracts, domain models, SQL design, fixture semantics |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Repository layout, milestone tasks, acceptance criteria, test strategy, CI/CD, Git and agent workflow |
| [ADRS.md](ADRS.md) | Initial architectural decisions, alternatives, consequences, revisit triggers |
| [DECISION_LOG.md](DECISION_LOG.md) | Assumptions, open decisions, implementation ledger and templates |
| [CLAUDE.md](CLAUDE.md) | Short repository instructions for Claude Code |
| [CLAUDE_CODE_KICKOFF.md](CLAUDE_CODE_KICKOFF.md) | Ready-to-paste initial coding assignment |

These documents define intended behavior. Commands and paths for application modules are implementation targets, not claims that the modules already exist. Explicit policy choices and numerical targets are project defaults, not industry standards or measured benchmarks. Exact dependency versions are resolved and locked during M0; no floating production dependencies are authorized.

## First usable outcome

With no model credentials, a developer starts PostgreSQL and the application, runs a healthy and faulty invoice suite against naive and resilient reference agents, inspects the fault/evidence timeline, strictly replays recorded interactions, and obtains an automated comparison report. Live model-backed experiments are a later, separately enabled phase.

## Developer quick start (M0 + M1)

Implemented so far: scenario compiler, local API (auth, scenarios, agents, runs with idempotency and a durable queue), PostgreSQL schema and migrations, CLI (`version`, `api`, `check`, `scenarios validate`, `dev init`). **No worker or agent executes yet**: accepted runs stay `queued` until M2. See [docs/progress.md](docs/progress.md).

Requires [uv](https://docs.astral.sh/uv/) and Docker (Linux containers). Exact versions: [docs/toolchain.md](docs/toolchain.md).

```text
uv sync --frozen --all-groups        # installs the locked environment (Python 3.12.13)
uv run agentcrash check              # ruff format/lint + mypy + generated-schema drift
uv run pytest tests/unit tests/property tests/contract   # no database needed
```

PostgreSQL (host port 15432, loopback only; data persists in the `agentcrash_pgdata` volume):

```text
cp .env.example .env                 # then set POSTGRES_PASSWORD (generation commands are in the file)
docker compose up -d --wait postgres
docker compose down                  # stops it; the volume and data are kept
```

Then migrate, seed and run the API (needs the PostgreSQL service above):

```text
uv run alembic upgrade head          # empty database -> current schema
uv run agentcrash dev init           # creates .local/token (never overwritten), seeds scenarios/agents
uv run agentcrash api                # http://127.0.0.1:8787 (loopback only; Bearer token from .local/token)
uv run pytest tests/integration      # real PostgreSQL, throwaway databases; fails if PostgreSQL is down
uv run python -m agentcrash.codegen  # regenerate schemas/ (check mode: --check)
uv run agentcrash scenarios validate scenarios/
```

`.env` and `.local/` are git-ignored. Compose refuses to start without `POSTGRES_PASSWORD`. If another service already uses the port, set `POSTGRES_HOST_PORT` in `.env`.
