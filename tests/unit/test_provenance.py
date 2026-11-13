"""Source and toolchain provenance (M4): a clean commit is reproducible, a dirty tree never is,
the dirty-state digest identifies the exact uncommitted state, and old manifests still load.
Real throwaway git repositories; nothing outside tmp_path is read."""

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentcrash import bootstrap
from agentcrash.bootstrap import make_provenance
from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.models import Manifest, SourceInfo, ToolchainInfo
from agentcrash.domain.registry import Registry
from agentcrash.replay.bundle import build_bundle, read_bundle
from support import CALLS, chained, synthetic_manifest

DIGEST = "sha256:" + "ab" * 32


def git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "code.py").write_text("print('v1')\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


def facts(root: Path, **env: str) -> tuple[SourceInfo, ToolchainInfo]:
    return make_provenance(root, env)()


def test_a_clean_committed_tree_is_reproducible(repo: Path) -> None:
    source, toolchain = facts(repo, AGENTCRASH_IMAGE_DIGEST=DIGEST)
    assert source.commit is not None and len(source.commit) == 40
    assert (source.dirty, source.patch_hash, source.reproducible) == (False, None, True)
    assert toolchain.image_digest == DIGEST and toolchain.platform


def test_a_modified_tracked_file_is_dirty_and_never_reproducible(repo: Path) -> None:
    (repo / "code.py").write_text("print('v2')\n")
    source, _ = facts(repo)
    assert source.dirty and not source.reproducible
    assert source.patch_hash is not None and len(source.patch_hash) == 64
    assert facts(repo)[0].patch_hash == source.patch_hash  # stable for the same state
    (repo / "code.py").write_text("print('v3')\n")
    assert facts(repo)[0].patch_hash != source.patch_hash  # a different edit, a different digest
    git(repo, "checkout", "--", "code.py")
    clean, _ = facts(repo)
    assert (clean.dirty, clean.patch_hash, clean.reproducible) == (False, None, True)


def test_untracked_files_make_the_tree_dirty_but_ignored_files_do_not(repo: Path) -> None:
    (repo / "ignored.txt").write_text("secret\n")
    assert facts(repo)[0].reproducible  # ignored by .gitignore: not part of the source
    (repo / "new.py").write_text("x = 1\n")
    first, _ = facts(repo)
    assert first.dirty and not first.reproducible and first.patch_hash is not None
    (repo / "new.py").write_text("x = 2\n")
    assert facts(repo)[0].patch_hash != first.patch_hash  # untracked content is in the digest
    before = facts(repo)[0].patch_hash
    (repo / "ignored.txt").write_text("changed\n")
    assert facts(repo)[0].patch_hash == before  # ignored files never enter the digest


def test_the_digest_never_contains_the_patch_text(repo: Path) -> None:
    (repo / "code.py").write_text("API_KEY = 'sk-live-not-a-real-key'\n")
    source, _ = facts(repo)
    assert "sk-live" not in source.model_dump_json()  # only a hash is recorded, no source text


def test_a_state_that_cannot_be_hashed_is_dirty_without_a_digest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "big.bin").write_bytes(b"0" * 100)
    monkeypatch.setattr(bootstrap, "_MAX_UNTRACKED_BYTES", 10)
    source, _ = facts(repo)
    assert (source.dirty, source.patch_hash, source.reproducible) == (True, None, False)
    monkeypatch.setattr(bootstrap, "_MAX_UNTRACKED_BYTES", 1 << 20)
    monkeypatch.setattr(bootstrap, "_MAX_UNTRACKED_FILES", 0)
    assert facts(repo)[0].patch_hash is None


def test_without_git_history_nothing_is_claimed(tmp_path: Path) -> None:
    source, toolchain = facts(tmp_path)  # not a repository
    assert (source.commit, source.dirty, source.patch_hash, source.reproducible) == (
        None,
        True,
        None,
        False,
    )
    assert toolchain.image_digest is None


@pytest.mark.parametrize("value", ["", "sha256:abc", "sha256:" + "AB" * 32, "latest", DIGEST + "x"])
def test_a_missing_or_malformed_image_digest_is_omitted(repo: Path, value: str) -> None:
    assert facts(repo, AGENTCRASH_IMAGE_DIGEST=value)[1].image_digest is None


def test_inconsistent_provenance_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError, match="INCONSISTENT_PROVENANCE"):
        SourceInfo(commit="a" * 40, dirty=True, reproducible=True)
    with pytest.raises(ValidationError, match="INCONSISTENT_PROVENANCE"):
        SourceInfo(commit=None, dirty=False, reproducible=True)
    with pytest.raises(ValidationError, match="INCONSISTENT_PROVENANCE"):
        SourceInfo(commit="a" * 40, dirty=False, patch_hash="c" * 64)
    with pytest.raises(ValidationError):
        ToolchainInfo(python="3.12", lock_hash=None, image_digest="sha256:short")


@pytest.fixture
def manifest(registry: Registry, doc: dict[str, Any]) -> Manifest:
    return synthetic_manifest(registry, doc)


def test_manifests_recorded_before_these_fields_still_load_and_claim_nothing(
    manifest: Manifest,
) -> None:
    old = copy.deepcopy(manifest.model_dump(mode="json"))
    for key in ("patch_hash", "reproducible"):
        del old["source"][key]
    for key in ("image_digest", "platform"):
        del old["toolchain"][key]
    loaded = Manifest.model_validate(old)
    assert loaded.source.reproducible is False and loaded.source.patch_hash is None
    assert loaded.toolchain.image_digest is None and loaded.toolchain.platform is None


def test_a_bundle_exported_before_these_fields_still_verifies(manifest: Manifest) -> None:
    import io
    import zipfile

    fresh = build_bundle(manifest, chained(), CALLS)
    with zipfile.ZipFile(io.BytesIO(fresh)) as zf:
        files = {i.filename: zf.read(i) for i in zf.infolist()}
    doc = json.loads(files["manifest.json"])
    for key in ("patch_hash", "reproducible"):
        del doc["source"][key]
    for key in ("image_digest", "platform"):
        del doc["toolchain"][key]
    files["manifest.json"] = json.dumps(doc).encode()
    index = json.loads(files["index.json"])
    index["manifest_hash"] = canonical_hash(doc)  # what the older exporter recorded
    import hashlib

    index["files"]["manifest.json"] = {
        "sha256": hashlib.sha256(files["manifest.json"]).hexdigest(),
        "size": len(files["manifest.json"]),
    }
    files["index.json"] = json.dumps(index).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    assert read_bundle(out.getvalue()).manifest.source.reproducible is False


def test_the_manifest_records_what_the_provider_reported(
    registry: Registry, doc: dict[str, Any], repo: Path
) -> None:
    from agentcrash.scenarios.compiler import compile_scenario
    from agentcrash.scenarios.manifest import build_manifest, validate_run_create
    from support import RUN

    (repo / "code.py").write_text("print('edited')\n")
    source, toolchain = facts(repo, AGENTCRASH_IMAGE_DIGEST=DIGEST)
    m = build_manifest(
        compile_scenario(copy.deepcopy(doc), registry),
        validate_run_create(RUN, registry),
        registry,
        source=source,
        toolchain=toolchain,
    )
    assert m.source.patch_hash == source.patch_hash and not m.source.reproducible
    assert m.toolchain.image_digest == DIGEST
    assert Manifest.model_validate(m.model_dump(mode="json")) == m  # round trip
