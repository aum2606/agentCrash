# Toolchain record

Support/version check date: **2026-11-09**. Host: Windows 11 (Docker Desktop, Linux containers). Everything in the "Tested" column was actually installed and run in M0 unless marked otherwise.

## Runtime and tooling

| Component | Baseline family (ADR-011) | Pinned / tested | Where pinned | Support evidence |
| --- | --- | --- | --- | --- |
| Python | 3.12 | **3.12.13** (uv-managed CPython) | `.python-version`, `requires-python = ">=3.12,<3.13"` | endoflife.date (2026-11-09): 3.12 EOL 2028-10-31, **security-fix phase only** (bug-fix support ended 2025-04-02). Latest 3.12 is 3.12.16 (2026-10-25); see deviation D-010 |
| uv | n/a | **0.11.6** | CI `setup-uv` `version: "0.11.6"`; `uv_build>=0.11.6,<0.12` | Newest on PyPI is 0.13.7; not adopted, to keep tested==pinned (D-011) |
| PostgreSQL | 18 | **18.6** (`postgres:18.6-trixie`) | `compose.yaml` | endoflife.date: 18 latest 18.6 (2026-08-11), EOL 2030-11-14 |
| Node.js | 24 LTS | **24.22.2** selected for M6; local host has 24.13.0 (used for the M6 slice 1 checks); CI `web` job uses `setup-node` `node-version: "24"` | `web/package.json` `engines` (>=24.13.0) | endoflife.date: 24 latest 24.22.2, EOL 2028-04-30. Newer LTS line 26 exists; baseline stays 24 per ADR-011 |
| pnpm | n/a | **10.33.2** | `web/package.json` `packageManager`; `web/pnpm-lock.yaml`; CI uses corepack | `minimumReleaseAge: 10080` (7 days) in `web/pnpm-workspace.yaml`: pnpm refuses a younger release. pnpm 12.18.0 exists; not adopted (tested==pinned) |
| React / react-dom | 19 | **19.3.1** | `web/pnpm-lock.yaml` | resolved 2026-11-14 under the 7-day rule |
| TypeScript | 6.0 | **6.0.3** | `web/package.json` `~6.0.3` | 7.0.2 exists but `typescript-eslint` 8.72 supports `<6.1.0`; `openapi-typescript` 7.13 declares `^5.x` (peer warning, generation tested) |
| Vite / Vitest / jsdom | 8 / 5 / 31 | **8.3.6 / 5.1.0 / 31.0.0** | `web/pnpm-lock.yaml` | newest releases (8.3.8, 31.0.1) were under 7 days old |
| ESLint / typescript-eslint | 10 / 8 | **10.14.0 / 8.72.1** | `web/pnpm-lock.yaml` | |
| openapi-typescript / openapi-fetch | 7 / 0.17 | **7.13.0 / 0.17.0** | `web/pnpm-lock.yaml` | generated `web/src/api/schema.d.ts`, drift-checked |
| Playwright | n/a | not added yet | | M6 browser slice |
| Docker Engine / Compose | n/a | 29.8.0 / 5.5.1 | host | n/a |

## Python dependencies (resolved in `uv.lock`, 47 packages)

Direct dependencies use `>=tested,<next-minor-or-major` ranges in `pyproject.toml`; `uv.lock` holds the exact resolution and CI installs with `--frozen` plus `uv lock --check`.

| Package | Locked | Group |
| --- | --- | --- |
| fastapi | 0.162.2 | runtime |
| starlette (transitive) | 1.10.0 | runtime |
| pydantic (transitive) | 2.14.3 | runtime |
| typer | 0.28.0 | runtime |
| sqlalchemy | 2.1.8 | runtime (sync Core; D-019) |
| psycopg[binary] | 3.3.8 | runtime (PostgreSQL driver) |
| alembic | 1.20.1 | runtime (migrations; also run by tests) |
| pyyaml | 6.0.3 | runtime (scenario loader; only `SafeLoader`-derived, tags/aliases rejected up front) |
| uvicorn | 0.54.0 | runtime |
| ruff | 0.17.4 | dev |
| mypy | 2.4.1 | dev |
| pytest | 9.2.0 | dev |
| hypothesis | 6.170.15 | dev (property tests) |
| types-pyyaml | 6.0.12.20261103 | dev (mypy stubs) |
| httpx2 | 2.16.0 | dev (FastAPI/Starlette `TestClient`; Starlette 1.10 deprecates `httpx` for it) |

Not yet added (later milestones): pytest-asyncio, Playwright. SQLAlchemy 2.1 is a newer minor than the 2.0 line most guides describe; only Core APIs that exist in both are used. No model SDK, Redis, Celery or Kubernetes dependency exists.

## Container images

| Image | Reference actually used | Digest (multi-arch index) |
| --- | --- | --- |
| PostgreSQL | `postgres:18.6-trixie` | `sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42` (Docker Hub, tag updated 2026-11-06; pulled and run locally) |

The digest is the manifest-list digest; the amd64 manifest is `sha256:e923a22ac074261dd73970c57edf659475bf0497cb9456f17d98ed7cdd3ca913`. The tag moves on rebuilds, so refresh tag and digest together via a reviewed PR.

## GitHub Actions (full commit SHA, resolved from the release tag on 2026-11-09)

| Action | Release | Commit SHA |
| --- | --- | --- |
| `actions/checkout` | v7.0.1 (2026-07-20) | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `astral-sh/setup-uv` | v10.3.3 (2026-10-28) | `68559e99cc807c1eb7e9238fd43a15bba460eec2` |

Both tags are lightweight tags, so the ref API returned the commit directly. Workflow permissions: `contents: read`; `persist-credentials: false`; no `pull_request_target`; no secrets.

## Verification limits

- Support dates come from endoflife.date (aggregating the vendors' own schedules), not a fresh read of each vendor page; no vendor security advisory feed was queried, and no dependency audit tool was run (the planned `supply-chain` job does not exist yet).
- Action SHAs were taken from the GitHub API for the latest release tags. Their contents were not code-reviewed.
- The CI workflow has not executed on GitHub (no remote exists). Its commands were run locally.
- Python 3.12.13 vs 3.12.16: uv 0.11.6 cannot download 3.12.16 (`No download found`). CI runs the same uv, so it also resolves 3.12.13.
