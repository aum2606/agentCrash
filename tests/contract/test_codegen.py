import json
from pathlib import Path

from agentcrash import codegen

ROOT = Path(__file__).resolve().parents[2]


def test_committed_schema_matches_models() -> None:
    assert codegen.main(["--check", "--root", str(ROOT)]) == 0


def test_drift_and_missing_file_are_detected(tmp_path: Path) -> None:
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 1  # missing
    assert codegen.main(["--root", str(tmp_path)]) == 0  # generate
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 0
    target = tmp_path / codegen.OUTPUT
    target.write_text(target.read_text(encoding="utf-8").replace("Scenario", "Scenari0", 1))
    assert codegen.main(["--check", "--root", str(tmp_path)]) == 1  # hand edit = drift


def test_schema_rejects_unknown_fields_and_encodes_effect_union() -> None:
    schemas = json.loads(codegen.render())["schemas"]
    scenario = schemas["Scenario"]
    assert scenario["additionalProperties"] is False
    effect = scenario["$defs"]["FaultRule"]["properties"]["effect"]
    assert effect["discriminator"]["propertyName"] == "type"
    assert len(effect["oneOf"]) == 8
