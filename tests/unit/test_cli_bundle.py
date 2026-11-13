"""Offline bundle commands: `agentcrash playback` and `agentcrash replay --strict`. No database, no
settings, no network; hostile bundle content never reaches the terminal unescaped."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentcrash.cli.main import app
from agentcrash.domain.models import Manifest
from agentcrash.domain.registry import Registry
from agentcrash.replay.bundle import build_bundle, write_bundle
from support import CALLS, chained, synthetic_manifest

runner = CliRunner()


@pytest.fixture
def manifest(registry: Registry, doc: dict[str, Any]) -> Manifest:
    return synthetic_manifest(registry, doc)


def bundle_file(tmp_path: Path, manifest: Manifest, **kw: Any) -> Path:
    events = kw.pop("events", None) or chained()
    return write_bundle(tmp_path, build_bundle(manifest, events, CALLS, [{"verdict": "pass"}]))


def run(*args: str) -> Any:
    return runner.invoke(app, list(args))


def test_playback_shows_the_recorded_evidence(tmp_path: Path, manifest: Manifest) -> None:
    result = run("playback", "--bundle", str(bundle_file(tmp_path, manifest)))
    assert result.exit_code == 0
    assert "invoice-429 v1" in result.output and "events    4" in result.output
    assert "verdict   pass" in result.output
    assert "tool.requested" in result.output and "tool=invoice.get" in result.output
    assert "call call-1" in result.output


def test_playback_json_is_a_pure_json_document(tmp_path: Path, manifest: Manifest) -> None:
    result = run("playback", "--bundle", str(bundle_file(tmp_path, manifest)), "--json")
    report = json.loads(result.output)
    assert result.exit_code == 0
    assert [e["seq"] for e in report["events"]] == [1, 2, 3, 4]
    assert report["run"]["replay_capability"] == "full" and len(report["bundle_sha256"]) == 64


def test_recorded_text_cannot_drive_the_terminal(tmp_path: Path, manifest: Manifest) -> None:
    hostile = "\x1b[2J\x1b]0;pwned\x07\r\nFAKE verdict   pass‮\x9b31m"
    path = bundle_file(tmp_path, manifest, events=chained(note=hostile))
    result = run("playback", "--bundle", str(path))
    assert result.exit_code == 0
    assert not any(c in result.output for c in "\x1b\x07\r\x9b‮")
    assert "\\x1b[2J" in result.output and "\\x0a" not in "".join(result.output.split("\n")[:1])
    assert not any(line.startswith("FAKE") for line in result.output.splitlines())


def test_a_strict_replay_of_a_recording_the_agent_would_not_make_diverges(
    tmp_path: Path, manifest: Manifest
) -> None:
    # The synthetic recording is not what the resilient agent does, so the first item differs.
    result = run("replay", "--bundle", str(bundle_file(tmp_path, manifest)), "--strict")
    assert result.exit_code == 1 and "REPLAY_DIVERGENCE at item 0" in result.output
    as_json = run("replay", "--bundle", str(bundle_file(tmp_path, manifest)), "--strict", "--json")
    doc = json.loads(as_json.stdout)
    assert as_json.exit_code == 1 and doc["matched"] is False
    assert doc["divergence"]["code"] == "REPLAY_DIVERGENCE"


def test_replay_requires_the_strict_flag(tmp_path: Path, manifest: Manifest) -> None:
    result = run("replay", "--bundle", str(bundle_file(tmp_path, manifest)))
    assert result.exit_code == 2 and "only --strict" in result.output


def test_an_incompatible_or_unfinished_recording_exits_2_with_its_reason(
    tmp_path: Path, manifest: Manifest
) -> None:
    partial = manifest.model_copy(update={"replay_capability": "partial"})
    result = run("replay", "--bundle", str(bundle_file(tmp_path, partial)), "--strict")
    assert result.exit_code == 2
    assert "REPLAY_INCOMPATIBLE: REPLAY_CAPABILITY_PARTIAL" in result.output
    unfinished = bundle_file(tmp_path, manifest, events=chained()[:3])
    result = run("replay", "--bundle", str(unfinished), "--strict")
    assert result.exit_code == 2 and "NOT_REPLAYABLE" in result.output
    # the same partial bundle is still fine to play back
    assert run("playback", "--bundle", str(bundle_file(tmp_path, partial))).exit_code == 0


def zip_with(tmp_path: Path, name: str, members: dict[str, bytes]) -> Path:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for member, body in members.items():
            zf.writestr(member, body)
    path = tmp_path / name
    path.write_bytes(out.getvalue())
    return path


@pytest.mark.parametrize("command", [["playback"], ["replay", "--strict"]])
def test_unsafe_or_damaged_bundles_exit_2_with_a_stable_code(
    tmp_path: Path, manifest: Manifest, command: list[str]
) -> None:
    good = bundle_file(tmp_path, manifest)
    flipped = bytearray(good.read_bytes())
    flipped[len(flipped) // 2] ^= 0xFF
    damaged = tmp_path / "damaged.acbundle"
    damaged.write_bytes(bytes(flipped))
    garbage = tmp_path / "garbage.acbundle"
    garbage.write_bytes(b"not an archive")
    cases = {
        tmp_path / "missing.acbundle": "BUNDLE_UNSAFE_PATH",
        tmp_path: "BUNDLE_UNSAFE_PATH",
        garbage: "BUNDLE_CORRUPT",
        zip_with(tmp_path, "trav.acbundle", {"../evil.py": b"import os"}): "BUNDLE_UNSAFE_PATH",
        zip_with(tmp_path, "exe.acbundle", {"evil.py": b"import os"}): "BUNDLE_UNKNOWN_FILE",
    }
    for path, code in cases.items():
        result = run(command[0], "--bundle", str(path), *command[1:])
        assert result.exit_code == 2 and f"error: {code}" in result.output, (path, result.output)
    result = run(command[0], "--bundle", str(damaged), *command[1:])
    assert result.exit_code == 2 and "error: BUNDLE_" in result.output


@pytest.mark.parametrize("command", [["playback"], ["replay", "--strict"]])
def test_commands_need_no_database_settings_or_network(
    tmp_path: Path, manifest: Manifest, command: list[str]
) -> None:
    path = bundle_file(tmp_path, manifest)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "AGENTCRASH_"))}
    env["AGENTCRASH_DATABASE_URL"] = "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none"
    env["AGENTCRASH_ROOT"] = str(tmp_path)  # no .env, no token, no fixtures here
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-c",
            "from agentcrash.cli.main import app; app()",
            *command,
            "--bundle",
            str(path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "Traceback" not in done.stderr and "psycopg" not in done.stderr
    assert done.returncode == (0 if command[0] == "playback" else 1), done.stderr
