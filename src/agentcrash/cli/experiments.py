"""`agentcrash experiments run | wait | gate`: drive an experiment through the local API and turn
the gate into an exit code (CONTRACTS.md section 4): 0 pass, 1 fail, 2 operational, configuration
or incomplete. A missing, unfinished, errored or cancelled run can never exit 0.

The commands only talk to the API with the local token; they never read the database and never run
agent code. The token is only sent to a loopback address (ADR-009)."""

import ipaddress
import json
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx2 as httpx
import typer

from agentcrash.bootstrap import read_token
from agentcrash.experiments.baseline import baseline_of, differences
from agentcrash.replay.playback import sanitize
from agentcrash.scenarios.loader import load_scenario_document
from agentcrash.settings import load_settings

experiments_app = typer.Typer(no_args_is_help=True, help="Experiments and the regression gate.")

OK, FAIL, OPERATIONAL = 0, 1, 2
EXIT = {"pass": OK, "fail": FAIL, "incomplete": OPERATIONAL}
DEFAULT_URL = "http://127.0.0.1:8787"


def pause(seconds: float) -> None:
    time.sleep(seconds)


def now() -> float:
    return time.monotonic()


class Operational(Exception):
    """Anything that stops the gate from being read: exit code 2."""


def make_client(api_url: str) -> httpx.Client:
    """Authenticated client for the local API. Tests replace this with an in-process client."""
    host = urlparse(api_url).hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise Operational(f"refusing non-loopback API address {api_url!r}: MVP is local-only")
    try:
        token = read_token(load_settings())
    except (OSError, ValueError) as exc:
        raise Operational(str(exc)) from None
    return httpx.Client(
        base_url=api_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    )


def _call(client: httpx.Client, method: str, path: str, **kw: Any) -> Any:
    try:
        response = client.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise Operational(f"cannot reach the API: {type(exc).__name__}") from None
    if response.status_code >= 400:
        try:
            error = response.json()["error"]
            what = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            what = "unexpected response"
        raise Operational(f"{response.status_code} {sanitize(what)}")
    return response.json()


def submit(client: httpx.Client, suite: Path, key: str | None) -> dict[str, Any]:
    """Post a suite file (YAML or JSON) as an experiment. The API validates it."""
    try:
        document = load_scenario_document(suite.read_bytes())
    except OSError as exc:
        raise Operational(f"cannot read {suite}: {exc.strerror}") from None
    except Exception as exc:  # ValidationFailed carries diagnostics
        raise Operational(f"{suite} is not a valid document: {sanitize(str(exc))}") from None
    result: dict[str, Any] = _call(
        client,
        "POST",
        "/api/v1/experiments",
        json=document,
        headers={"Idempotency-Key": key or str(uuid4())},
    )
    return result


def wait_finished(
    client: httpx.Client,
    experiment_id: str,
    timeout: float,
    interval: float = 2.0,
) -> dict[str, Any]:
    deadline = now() + timeout
    while True:
        record: dict[str, Any] = _call(client, "GET", f"/api/v1/experiments/{experiment_id}")
        if record["status"] != "running":
            return record
        if now() >= deadline:
            raise Operational(f"experiment {experiment_id} still running after {timeout:g}s")
        pause(interval)


def fetch_report(client: httpx.Client, experiment_id: str, partial: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = _call(
        client,
        "GET",
        f"/api/v1/experiments/{experiment_id}/report",
        params={"partial": "true"} if partial else None,
    )
    return report


def render(report: dict[str, Any]) -> list[str]:
    gate = report["gate"]
    cand = report["counts_by_agent"]["candidate"]
    base = report["counts_by_agent"]["baseline"]
    lines = [
        f"gate: {gate['status']}  ({report['gate_policy_id']})",
        f"pairs: {report['complete_pairs']}/{report['expected_pairs']} complete",
        f"candidate: {cand['passed']} passed, {cand['failed']} failed, "
        f"{cand['inconclusive']} inconclusive, {cand['errored']} errored, "
        f"{cand['cancelled']} cancelled, {cand['unfinished']} unfinished",
        f"baseline:  {base['passed']} passed, {base['failed']} failed (never gates)",
    ]
    lines += [f"  {r['code']}: {sanitize(r['detail'])}" for r in gate["reasons"]]
    return lines


def _drift(report: dict[str, Any], baseline: Path) -> list[str]:
    try:
        expected = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Operational(f"cannot read baseline {baseline}: {type(exc).__name__}") from None
    return differences(expected, baseline_of(report))


def _emit(report: dict[str, Any], as_json: bool, baseline: Path | None = None) -> int:
    """Gate exit code, except that a passing gate whose results no longer match the reviewed
    baseline is a failure: an unexplained change is never silently accepted."""
    code = EXIT[report["gate"]["status"]]
    drift = _drift(report, baseline) if baseline is not None else []
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        for line in render(report):
            typer.echo(line)
    for line in drift[:20]:
        typer.echo(f"  BASELINE_DRIFT {sanitize(line)}", err=True)
    return FAIL if drift and code == OK else code


def _operational(exc: Operational) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(OPERATIONAL)


ApiUrl = Annotated[str, typer.Option("--api-url", help="Loopback API address.")]
AsJson = Annotated[bool, typer.Option("--json", help="Machine-readable JSON on stdout.")]
Baseline = Annotated[
    Path | None, typer.Option("--baseline", help="Reviewed baseline file; drift fails a pass.")
]
Timeout = Annotated[float, typer.Option(help="Seconds to wait for the runs to finish.")]


@experiments_app.command("run")
def run_experiment(
    suite: Annotated[Path, typer.Option("--suite", help="Experiment request, YAML or JSON.")],
    wait: Annotated[bool, typer.Option("--wait", help="Wait, then print the gate.")] = False,
    json_output: AsJson = False,
    key: Annotated[str | None, typer.Option(help="Idempotency key (default: a new one).")] = None,
    timeout: Timeout = 600.0,
    baseline: Baseline = None,
    api_url: ApiUrl = DEFAULT_URL,
) -> None:
    """Submit an experiment. With --wait, exit 0 gate pass, 1 gate fail, 2 anything else."""
    try:
        with make_client(api_url) as client:
            created = submit(client, suite, key)
            if not wait:
                typer.echo(json.dumps(created, indent=2) if json_output else created["id"])
                raise typer.Exit(OK)
            wait_finished(client, created["id"], timeout)
            raise typer.Exit(_emit(fetch_report(client, created["id"]), json_output, baseline))
    except Operational as exc:
        raise _operational(exc) from None


@experiments_app.command("wait")
def wait_experiment(
    experiment_id: Annotated[str, typer.Argument(help="Experiment id.")],
    timeout: Timeout = 600.0,
    api_url: ApiUrl = DEFAULT_URL,
) -> None:
    """Wait until every run is finished. Exit 0 finished (read the gate separately), 2 not."""
    try:
        with make_client(api_url) as client:
            record = wait_finished(client, experiment_id, timeout)
            typer.echo(f"experiment {experiment_id}: {record['status']}")
    except Operational as exc:
        raise _operational(exc) from None


@experiments_app.command("gate")
def gate_experiment(
    experiment_id: Annotated[str, typer.Argument(help="Experiment id.")],
    json_output: AsJson = False,
    baseline: Baseline = None,
    api_url: ApiUrl = DEFAULT_URL,
) -> None:
    """Read the gate. Exit 0 pass, 1 fail or baseline drift, 2 incomplete or operational."""
    try:
        with make_client(api_url) as client:
            raise typer.Exit(_emit(fetch_report(client, experiment_id), json_output, baseline))
    except Operational as exc:
        raise _operational(exc) from None


@experiments_app.command("baseline")
def baseline_experiment(
    experiment_id: Annotated[str, typer.Argument(help="Experiment id.")],
    output: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
    api_url: ApiUrl = DEFAULT_URL,
) -> None:
    """Print the machine-independent baseline view of a finished, passing experiment. It never
    replaces a committed baseline by itself: pass --output deliberately and review the diff."""
    try:
        with make_client(api_url) as client:
            report = fetch_report(client, experiment_id)
        if report["gate"]["status"] != "pass":
            status = report["gate"]["status"]
            raise Operational(f"gate is {status}; only a passing run can be a baseline")
        text = json.dumps(baseline_of(report), indent=2, sort_keys=True) + "\n"
        if output is None:
            typer.echo(text, nl=False)
        else:
            output.write_text(text, encoding="utf-8", newline="\n")
            typer.echo(f"wrote {output}")
    except Operational as exc:
        raise _operational(exc) from None
