"""Composition root: assembles the trusted registry from shipped files. The only module that
may combine environments, agents and repository data files."""

import contextlib
import json
import os
import platform
import re
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from agentcrash.agents.registry import AGENTS
from agentcrash.api.app import Services, create_app
from agentcrash.domain.canonical import canonical_hash, sha256_hex
from agentcrash.domain.models import GraderId, SourceInfo, ToolchainInfo
from agentcrash.domain.registry import EnvironmentSpec, GraderSpec, Registry
from agentcrash.environments.invoice import catalog
from agentcrash.environments.invoice.fixture import load_bundle
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.settings import Settings, load_settings
from agentcrash.storage.attempts import AttemptRepo
from agentcrash.storage.engine import make_engine
from agentcrash.storage.queue import JobQueue
from agentcrash.storage.store import PostgresStore
from agentcrash.workers.loop import Worker
from agentcrash.workers.supervisor import AttemptSupervisor

_MAX_FILE = 1 << 20


class _GraderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: GraderId
    version: Annotated[int, Field(ge=1)]
    always_required: bool
    params: dict[str, str]


class _GraderFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str
    graders: list[_GraderEntry]


def _read_json(path: Path) -> object:
    if path.stat().st_size > _MAX_FILE:
        raise ValueError(f"{path} exceeds {_MAX_FILE} bytes")
    return json.loads(path.read_text(encoding="utf-8"))


def default_registry(root: Path) -> Registry:
    """``root`` is the repository root holding ``fixtures/`` and ``graders/``."""
    fixtures = {
        fixture_id: load_bundle(root / rel).hash
        for fixture_id, rel in catalog.FIXTURE_FILES.items()
    }
    env = EnvironmentSpec(
        id=catalog.ENVIRONMENT_ID,
        version=catalog.ENVIRONMENT_VERSION,
        tasks=catalog.TASKS,
        actors=catalog.ACTORS,
        fixtures=fixtures,
        tools=catalog.TOOLS,
    )
    gf = _GraderFile.model_validate(_read_json(root / "graders" / "registry.json"))
    graders = {g.id: GraderSpec(g.id, g.version, g.always_required, g.params) for g in gf.graders}
    bundles = [load_bundle(root / rel) for rel in catalog.FIXTURE_FILES.values()]
    snapshots = {
        sid: snap["tool"]
        for b in bundles
        for sid, snap in b.faults.get("stale_snapshots", {}).items()
    }
    return Registry(
        environments={(env.id, env.version): env},
        graders=graders,
        agents=dict(AGENTS),
        stale_snapshots=snapshots,
        conflict_patches=frozenset(
            pid for b in bundles for pid in b.faults.get("conflict_patches", {})
        ),
        document_fixtures=frozenset(did for b in bundles for did in b.faults.get("documents", {})),
    )


def alembic_head(root: Path) -> str:
    head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()
    if head is None:
        raise RuntimeError("no Alembic revisions found")
    return head


def make_provenance(root: Path) -> Callable[[], tuple[SourceInfo, ToolchainInfo]]:
    """Source/toolchain facts recorded in every manifest. A tree with uncommitted or untracked
    files, or one where Git cannot be consulted, is reported dirty (not reproducible)."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def provenance() -> tuple[SourceInfo, ToolchainInfo]:
        commit: str | None = None
        dirty = True
        try:
            head = git("rev-parse", "HEAD")
            if head.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
                commit = head.stdout.strip()
            status = git("status", "--porcelain")
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        lock = root / "uv.lock"
        lock_hash = sha256_hex(lock.read_bytes()) if lock.is_file() else None
        return (
            SourceInfo(commit=commit, dirty=dirty),
            ToolchainInfo(python=platform.python_version(), lock_hash=lock_hash),
        )

    return provenance


def read_token(settings: Settings) -> str:
    try:
        token = settings.token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise FileNotFoundError("API token missing; run `agentcrash dev init`") from None
    if len(token) < 32:
        raise ValueError(
            f"API token in {settings.token_path} is too short; delete it and re-run init"
        )
    return token


def build_services(settings: Settings) -> Services:
    return Services(
        store=PostgresStore(make_engine(settings.database_url)),
        registry=default_registry(settings.root),
        token=read_token(settings),
        expected_revision=alembic_head(settings.root),
        provenance=make_provenance(settings.root),
        queue_cap=settings.queue_cap,
        create_rate_per_minute=settings.create_rate_per_minute,
    )


def build_worker(settings: Settings) -> Worker:
    """Wire the worker: queue, per-attempt storage, supervisor (child runner) and grading."""
    engine = make_engine(settings.database_url)
    owner = f"worker-{os.getpid()}-{secrets.token_hex(4)}"
    queue = JobQueue(engine)
    repo = AttemptRepo(engine)
    supervisor = AttemptSupervisor(queue, repo, settings.root, owner, dict(catalog.TOOLS))
    return Worker(queue, repo, supervisor, owner)


def build_app() -> FastAPI:
    """uvicorn factory: ``agentcrash.bootstrap:build_app``."""
    return create_app(build_services(load_settings()))


@dataclass(frozen=True)
class InitReport:
    token_created: bool
    scenarios_created: int
    scenarios_unchanged: int
    agents: int


def dev_init(settings: Settings) -> InitReport:
    """Create the local token if absent (never overwritten), then idempotently seed agent and
    scenario versions. Requires a migrated database; never alters or drops tables."""
    token_created = False
    if not settings.token_path.exists():
        settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings.token_path, "x", encoding="utf-8") as fh:  # 'x': never overwrite
            fh.write(secrets.token_urlsafe(32) + chr(10))
        token_created = True
        with contextlib.suppress(OSError):  # best effort on filesystems without POSIX modes
            os.chmod(settings.token_path, 0o600)
    services = build_services(settings)
    revision = services.store.schema_revision()
    if revision != services.expected_revision:
        raise RuntimeError(
            f"database revision is {revision!r}, expected {services.expected_revision!r}; "
            "run `uv run alembic upgrade head` first"
        )
    for spec in services.registry.agents.values():
        schema_hash = canonical_hash(spec.config_model.model_json_schema())
        services.store.ensure_agent_version(
            spec.id, spec.version, spec.id, schema_hash, spec.source_hash
        )
    created = unchanged = 0
    for path in sorted((settings.root / "scenarios").rglob("*.yaml")):
        compiled = compile_scenario(load_scenario_document(path.read_bytes()), services.registry)
        if services.store.put_scenario(compiled).created:
            created += 1
        else:
            unchanged += 1
    return InitReport(token_created, created, unchanged, len(services.registry.agents))
