"""CLI entry point: version, api, check, scenarios validate, dev init, playback, replay."""

import ipaddress
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from agentcrash import __version__
from agentcrash.agents.registry import ADAPTERS
from agentcrash.agents.rng import SeededRng
from agentcrash.bootstrap import alembic_head, build_worker, default_registry, dev_init, read_token
from agentcrash.domain.diagnostics import ValidationFailed
from agentcrash.replay.bundle import Bundle, BundleError, read_bundle
from agentcrash.replay.engine import NotReplayable
from agentcrash.replay.playback import playback_report, render_text, sanitize
from agentcrash.replay.service import Incompatible, replay_recorded
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.settings import load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
scenarios_app = typer.Typer(no_args_is_help=True, help="Scenario tools.")
app.add_typer(scenarios_app, name="scenarios")
dev_app = typer.Typer(no_args_is_help=True, help="Local development setup.")
app.add_typer(dev_app, name="dev")

# Python only: ruff would otherwise reformat code fences in the normative handoff docs.
PY_PATHS = ["src", "tests", "migrations"]

# Static checks currently implemented. Later milestones append theirs here.
CHECKS: list[tuple[str, list[str]]] = [
    ("ruff format", ["-m", "ruff", "format", "--check", *PY_PATHS]),
    ("ruff lint", ["-m", "ruff", "check", *PY_PATHS]),
    ("mypy", ["-m", "mypy"]),
    ("generated schemas", ["-m", "agentcrash.codegen", "--check"]),
]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Loopback only (ADR-009).")] = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Run the local API."""
    if not _is_loopback(host):
        typer.echo(f"refusing non-loopback host {host!r}: MVP is local-only (ADR-009)", err=True)
        raise typer.Exit(2)
    try:
        read_token(load_settings())  # fail fast with a clear message instead of a traceback
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    uvicorn.run("agentcrash.bootstrap:build_app", factory=True, host=host, port=port)


@dev_app.command("init")
def dev_init_command() -> None:
    """Create the local API token (never overwritten) and seed scenario/agent versions.

    Needs PostgreSQL running and migrated (`uv run alembic upgrade head`)."""
    try:
        report = dev_init(load_settings())
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(
        f"token {'created' if report.token_created else 'kept'}; "
        f"scenarios created={report.scenarios_created} unchanged={report.scenarios_unchanged}; "
        f"agents={report.agents}"
    )


@scenarios_app.command("validate")
def scenarios_validate(
    path: Annotated[Path, typer.Argument(help="Scenario file or directory of *.yaml/*.yml.")],
    root: Annotated[Path, typer.Option(help="Repository root with fixtures/ and graders/.")] = Path(
        "."
    ),
    json_output: Annotated[bool, typer.Option("--json", help="JSON report on stdout.")] = False,
) -> None:
    """Compile scenarios offline. Exit 0 all valid, 1 some invalid, 2 operational error."""
    try:
        registry = default_registry(root)
        files = sorted([path] if path.is_file() else [*path.rglob("*.yaml"), *path.rglob("*.yml")])
        if not files:
            raise FileNotFoundError(f"no scenario files under {path}")
        results = []
        for f in files:
            try:
                compiled = compile_scenario(load_scenario_document(f.read_bytes()), registry)
                results.append(
                    {"file": f.as_posix(), "valid": True, "content_hash": compiled.content_hash}
                )
            except ValidationFailed as exc:
                results.append(
                    {
                        "file": f.as_posix(),
                        "valid": False,
                        "code": exc.code,
                        "details": [d.as_dict() for d in exc.details],
                    }
                )
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(json.dumps({"results": results}, indent=2))
    else:
        for r in results:
            typer.echo(f"{'ok     ' if r['valid'] else 'INVALID'} {r['file']}", err=not r["valid"])
            for d in r.get("details", []):  # type: ignore[attr-defined]
                typer.echo(f"        {d['path'] or '/'}: {d['code']}", err=True)
    if not all(r["valid"] for r in results):
        raise typer.Exit(1)


def _open_bundle(path: Path) -> Bundle:
    """Offline and read-only: no settings, no database. Unsafe or damaged bundles exit 2."""
    try:
        return read_bundle(path)
    except BundleError as err:
        typer.echo(f"error: {err.code}: {sanitize(err.detail)}", err=True)
        raise typer.Exit(2) from None


@app.command()
def playback(
    bundle: Annotated[Path, typer.Option("--bundle", help="An exported .acbundle file.")],
    json_output: Annotated[bool, typer.Option("--json", help="JSON report on stdout.")] = False,
) -> None:
    """Show a bundle's recorded evidence. Read-only; runs no agent and no tool."""
    report = playback_report(_open_bundle(bundle))
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        for line in render_text(report):
            typer.echo(line)


@app.command()
def replay(
    bundle: Annotated[Path, typer.Option("--bundle", help="An exported .acbundle file.")],
    strict: Annotated[
        bool, typer.Option("--strict", help="Strict recorded-response replay.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="JSON result on stdout.")] = False,
) -> None:
    """Strictly replay a bundle with the installed adapter. Exit 0 the agent matched the
    recording, 1 it diverged, 2 the bundle is unsafe or the replay is not possible."""
    if not strict:
        typer.echo("error: only --strict replay exists; reruns are a separate operation", err=True)
        raise typer.Exit(2)
    opened = _open_bundle(bundle)
    try:
        result = replay_recorded(
            opened.manifest,
            opened.events,
            opened.calls,
            ADAPTERS,
            SeededRng,
        )
    except Incompatible as err:
        typer.echo(f"error: REPLAY_INCOMPATIBLE: {', '.join(err.reasons)}", err=True)
        raise typer.Exit(2) from None
    except NotReplayable as err:
        typer.echo(f"error: NOT_REPLAYABLE: {sanitize(str(err))}", err=True)
        raise typer.Exit(2) from None
    divergence = None if result.divergence is None else asdict(result.divergence)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "matched": result.ok,
                    "consumed": result.consumed,
                    "total": result.total,
                    "divergence": divergence,
                },
                indent=2,
            )
        )
    elif result.ok:
        typer.echo(f"matched: {result.consumed}/{result.total} recorded items consumed")
    else:
        typer.echo(
            f"REPLAY_DIVERGENCE at item {divergence['position']} ({divergence['kind']}): "  # type: ignore[index]
            f"{sanitize(divergence['detail'])}; consumed {result.consumed}/{result.total}",  # type: ignore[index]
            err=True,
        )
    if not result.ok:
        raise typer.Exit(1)


@app.command()
def worker() -> None:
    """Run the execution/grading worker until interrupted (Ctrl+C drains, then recovers)."""
    import logging
    import signal
    import threading

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    settings = load_settings()
    w = build_worker(settings)
    revision = w.repo.schema_revision()
    if revision != alembic_head(settings.root):
        typer.echo(
            f"error: database revision is {revision!r}; run `uv run alembic upgrade head` first",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(f"worker {w.owner} started")
    w.run_forever(stop)
    typer.echo("worker stopped")


@app.command()
def check() -> None:
    """Run the static checks (format, lint, types). Tests: `uv run pytest`."""
    failed = []
    for name, args in CHECKS:
        typer.echo(f"==> {name}", err=True)
        if subprocess.run([sys.executable, *args], check=False).returncode != 0:  # noqa: S603
            failed.append(name)
    if failed:
        typer.echo(f"check FAILED: {', '.join(failed)}", err=True)
        raise typer.Exit(1)
    typer.echo("check passed (static only; pytest and integration tests are separate commands)")


if __name__ == "__main__":
    app()
