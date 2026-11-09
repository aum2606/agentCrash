import json
from pathlib import Path

from typer.testing import CliRunner

from agentcrash.cli.main import app

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "scenarios" / "invoice" / "v1"
runner = CliRunner()


def validate(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["scenarios", "validate", *args, "--root", str(ROOT)])
    return result.exit_code, result.stdout


def test_shipped_scenarios_are_valid() -> None:
    code, out = validate(str(SCENARIOS), "--json")
    report = json.loads(out)  # stdout must be pure JSON
    assert code == 0
    assert [r["valid"] for r in report["results"]] == [True] * len(report["results"])
    assert all(len(r["content_hash"]) == 64 for r in report["results"])


def test_invalid_scenario_exits_1_with_stable_codes(tmp_path: Path) -> None:
    text = (SCENARIOS / "invoice-429.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text.replace('probability: "1"', 'probability: "2"'), encoding="utf-8")
    code, out = validate(str(bad), "--json")
    result = json.loads(out)["results"][0]
    assert code == 1
    assert result["code"] == "SCENARIO_VALIDATION_FAILED"
    assert {"path": "/faults/0/probability", "code": "INVALID_PROBABILITY"}.items() <= result[
        "details"
    ][0].items()


def test_malicious_yaml_tag_is_rejected_not_executed(tmp_path: Path) -> None:
    evil = tmp_path / "evil.yaml"
    evil.write_text("x: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    code, out = validate(str(evil), "--json")
    assert code == 1
    assert json.loads(out)["results"][0]["details"][0]["code"] == "YAML_TAG_FORBIDDEN"


def test_operational_errors_exit_2(tmp_path: Path) -> None:
    assert validate(str(tmp_path / "missing"))[0] == 2
    assert validate(str(tmp_path))[0] == 2  # directory with no scenarios
