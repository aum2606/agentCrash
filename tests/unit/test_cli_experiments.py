"""`agentcrash experiments run | wait | gate` against a canned API: exit codes follow the gate and
nothing but a passing gate exits 0."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from typer.testing import CliRunner

from agentcrash.cli import experiments as cli
from agentcrash.cli.main import app

runner = CliRunner()
SUITE = "name: demo\nseeds: ['1']\n"
ID = "11111111-1111-1111-1111-111111111111"


def report(status: str, reasons: list[dict[str, str]] | None = None) -> dict[str, Any]:
    counts = {
        "passed": 10,
        "failed": 0,
        "inconclusive": 0,
        "errored": 0,
        "cancelled": 0,
        "unfinished": 0,
    }
    return {
        "gate": {"status": status, "reasons": reasons or []},
        "gate_policy_id": "mock-release.v1",
        "complete_pairs": 10,
        "expected_pairs": 10,
        "counts_by_agent": {"candidate": counts, "baseline": {**counts, "failed": 3}},
    }


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "no\x1b[31m"}})


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        monkeypatch.setattr(
            cli,
            "make_client",
            lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handler)),
        )

    return install


def serve(rep: httpx.Response, statuses: list[str] | None = None) -> Callable[..., httpx.Response]:
    left = list(statuses or ["completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(202, json={"id": ID, "status": "running"})
        if path.endswith("/report"):
            return rep
        return httpx.Response(
            200, json={"id": ID, "status": left.pop(0) if len(left) > 1 else left[0]}
        )

    return handler


@pytest.mark.parametrize(("status", "code"), [("pass", 0), ("fail", 1), ("incomplete", 2)])
def test_the_gate_status_is_the_exit_code(api: Callable[..., None], status: str, code: int) -> None:
    api(serve(httpx.Response(200, json=report(status))))
    result = runner.invoke(app, ["experiments", "gate", ID])
    assert result.exit_code == code
    assert f"gate: {status}" in result.output


def test_json_output_is_the_report_itself(api: Callable[..., None]) -> None:
    api(serve(httpx.Response(200, json=report("pass"))))
    result = runner.invoke(app, ["experiments", "gate", ID, "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout) == report("pass")


def test_reasons_are_listed_and_escaped(api: Callable[..., None]) -> None:
    reasons = [{"code": "CANDIDATE_REQUIRED_FAILURE", "detail": "case \x1b[2Jone"}]
    api(serve(httpx.Response(200, json=report("fail", reasons))))
    result = runner.invoke(app, ["experiments", "gate", ID])
    assert result.exit_code == 1 and "CANDIDATE_REQUIRED_FAILURE" in result.output
    # typer.echo strips ANSI on its own when not a terminal, so check the rendered lines.
    assert "\x1b" not in "\n".join(cli.render(report("fail", reasons)))


def test_each_run_gets_a_fresh_idempotency_key_unless_one_is_given(
    api: Callable[..., None], tmp_path: Path
) -> None:
    keys: list[str] = []
    inner = serve(httpx.Response(200, json=report("pass")))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            keys.append(request.headers["Idempotency-Key"])
        return inner(request)

    api(handler)
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE)
    for extra in ([], [], ["--key", "mine"]):
        runner.invoke(app, ["experiments", "run", "--suite", str(suite), *extra])
    assert keys[0] != keys[1] and keys[2] == "mine"


@pytest.mark.parametrize(
    "response",
    [error(409, "EXPERIMENT_NOT_FINISHED"), error(404, "EXPERIMENT_NOT_FOUND"), error(401, "X")],
)
def test_an_unreadable_gate_never_exits_zero(api: Callable[..., None], response: Any) -> None:
    api(serve(response))
    result = runner.invoke(app, ["experiments", "gate", ID])
    assert result.exit_code == 2
    assert "\x1b" not in result.output


def test_an_unreachable_api_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(refuse)),
    )
    assert runner.invoke(app, ["experiments", "gate", ID]).exit_code == 2


def test_the_token_is_never_sent_to_a_non_loopback_address() -> None:
    result = runner.invoke(app, ["experiments", "gate", ID, "--api-url", "http://example.com:8787"])
    assert result.exit_code == 2 and "non-loopback" in result.output


def test_run_without_wait_prints_the_id(api: Callable[..., None], tmp_path: Path) -> None:
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE)
    api(serve(httpx.Response(200, json=report("pass"))))
    result = runner.invoke(app, ["experiments", "run", "--suite", str(suite)])
    assert (result.exit_code, result.stdout.strip()) == (0, ID)


def test_run_wait_polls_until_finished_then_gates(
    api: Callable[..., None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(cli, "pause", waits.append)
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE)
    api(serve(httpx.Response(200, json=report("fail")), ["running", "running", "completed"]))
    result = runner.invoke(app, ["experiments", "run", "--suite", str(suite), "--wait"])
    assert result.exit_code == 1 and len(waits) == 2


def test_run_wait_times_out_with_exit_two(
    api: Callable[..., None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(range(0, 1000, 100))
    monkeypatch.setattr(cli, "now", lambda: next(clock))
    monkeypatch.setattr(cli, "pause", lambda s: None)
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE)
    api(serve(httpx.Response(200, json=report("pass")), ["running"]))
    result = runner.invoke(
        app, ["experiments", "run", "--suite", str(suite), "--wait", "--timeout", "250"]
    )
    assert result.exit_code == 2 and "still running" in result.output


def test_a_missing_or_malformed_suite_exits_two(api: Callable[..., None], tmp_path: Path) -> None:
    api(serve(httpx.Response(200, json=report("pass"))))
    assert (
        runner.invoke(app, ["experiments", "run", "--suite", str(tmp_path / "no.yaml")]).exit_code
        == 2
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unterminated")
    assert runner.invoke(app, ["experiments", "run", "--suite", str(bad)]).exit_code == 2


def test_wait_reports_the_final_status(api: Callable[..., None]) -> None:
    api(serve(httpx.Response(200, json=report("pass")), ["completed"]))
    result = runner.invoke(app, ["experiments", "wait", ID])
    assert result.exit_code == 0 and "completed" in result.output
