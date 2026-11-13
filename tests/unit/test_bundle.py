"""Export bundle: round trip, determinism, and refusal of every hostile or damaged archive with a
stable reason code. Pure: synthetic chained events, no database, nothing extracted to disk."""

import hashlib
import io
import json
import stat
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.events import StoredEvent
from agentcrash.domain.models import Manifest
from agentcrash.domain.registry import Registry
from agentcrash.replay import bundle as bundle_mod
from agentcrash.replay.bundle import BundleError, build_bundle, read_bundle, write_bundle
from support import CALLS, chained, synthetic_manifest


@pytest.fixture
def manifest(registry: Registry, doc: dict[str, Any]) -> Manifest:
    return synthetic_manifest(registry, doc)


@pytest.fixture
def good(manifest: Manifest) -> bytes:
    return build_bundle(
        manifest, chained(), CALLS, [{"verdict": "pass"}], {"initial": {"a": 1}}, [{"r": 1}]
    )


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def archive(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return out.getvalue()


def reindexed(data: bytes, edit: Callable[[dict[str, bytes]], None]) -> bytes:
    """Edit members, then rewrite the checksum index so only deeper checks can notice."""
    files = members(data)
    edit(files)
    index = json.loads(files["index.json"])
    index["files"] = {
        n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
        for n, b in files.items()
        if n != "index.json"
    }
    files["index.json"] = json.dumps(index).encode()
    return archive(files)


def code_of(data: bytes | Path) -> str:
    with pytest.raises(BundleError) as err:
        read_bundle(data)
    return err.value.code


# --- round trip and content addressing --------------------------------------------------------


def test_a_bundle_reads_back_exactly_and_is_content_addressed(
    manifest: Manifest, good: bytes
) -> None:
    b = read_bundle(good)
    assert b.bundle_sha256 == hashlib.sha256(good).hexdigest()
    assert b.manifest == manifest
    assert b.events == chained() and b.calls == CALLS
    assert b.evaluations == [{"verdict": "pass"}] and b.snapshots == {"initial": {"a": 1}}
    assert b.receipts == [{"r": 1}]
    assert b.index["manifest_hash"] == canonical_hash(manifest.model_dump(mode="json"))


def test_identical_evidence_gives_identical_bytes_and_changed_evidence_a_new_address(
    manifest: Manifest, good: bytes
) -> None:
    assert (
        build_bundle(
            manifest, chained(), CALLS, [{"verdict": "pass"}], {"initial": {"a": 1}}, [{"r": 1}]
        )
        == good
    )
    other = build_bundle(manifest, chained(), CALLS)
    assert hashlib.sha256(other).hexdigest() != hashlib.sha256(good).hexdigest()


def test_write_bundle_publishes_by_hash_atomically_and_idempotently(
    good: bytes, tmp_path: Path
) -> None:
    path = write_bundle(tmp_path / "out", good)
    assert path.name == f"{hashlib.sha256(good).hexdigest()}.acbundle"
    assert write_bundle(tmp_path / "out", good) == path
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind
    assert read_bundle(path).bundle_sha256 == hashlib.sha256(good).hexdigest()


def test_a_broken_event_chain_is_not_exported(manifest: Manifest) -> None:
    events = chained()
    events[1] = StoredEvent(**{**events[1].__dict__, "payload": {"note": "edited"}})
    with pytest.raises(BundleError) as err:
        build_bundle(manifest, events, CALLS)
    assert err.value.code == "BUNDLE_EVENT_CHAIN"


# --- hostile archives -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../evil.json",
        "/etc/passwd",
        "sub/events.ndjson",
        "C:evil.json",
        "..\\evil",
        ".hidden",
        "d/",
    ],
)
def test_unsafe_member_names_are_refused(good: bytes, name: str) -> None:
    assert code_of(archive({**members(good), name: b"x"})) == "BUNDLE_UNSAFE_PATH"


def test_unknown_files_including_executable_content_are_refused(good: bytes) -> None:
    for name in ("evil.py", "plugin.so", "model.pkl", "setup.cfg"):
        assert code_of(archive({**members(good), name: b"x"})) == "BUNDLE_UNKNOWN_FILE"


def test_a_symlink_member_is_refused(good: bytes) -> None:
    only = io.BytesIO()
    with zipfile.ZipFile(only, "w") as zf:
        link = zipfile.ZipInfo("manifest.json")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, b"/etc/passwd")
    assert code_of(only.getvalue()) == "BUNDLE_SYMLINK"


def test_a_duplicate_member_is_refused(good: bytes) -> None:
    out = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(out, "w") as zf:
        warnings.simplefilter("ignore")
        for name, body in members(good).items():
            zf.writestr(name, body)
        zf.writestr("events.ndjson", b"")
    assert code_of(out.getvalue()) == "BUNDLE_DUPLICATE_ENTRY"


def test_an_encrypted_member_is_refused(good: bytes) -> None:
    data = bytearray(good)
    at = data.find(b"PK")  # central directory entry: general purpose flags at +8
    while at != -1:
        data[at + 8] |= 0x1
        at = data.find(b"PK", at + 1)
    assert code_of(bytes(data)) == "BUNDLE_UNSUPPORTED_ENTRY"


def test_other_compression_methods_are_refused(good: bytes) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_BZIP2) as zf:
        for name, body in members(good).items():
            zf.writestr(name, body)
    assert code_of(out.getvalue()) == "BUNDLE_UNSUPPORTED_ENTRY"


# --- size limits ------------------------------------------------------------------------------


def test_oversized_archives_members_and_totals_are_refused(
    good: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "MAX_ARCHIVE_BYTES", len(good) - 1)
        assert code_of(good) == "BUNDLE_TOO_LARGE"
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "MAX_MEMBER_BYTES", 10)
        assert code_of(good) == "BUNDLE_TOO_LARGE"
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "MAX_TOTAL_BYTES", sum(len(b) for b in members(good).values()) - 1)
        assert code_of(good) == "BUNDLE_TOO_LARGE"


def test_a_member_that_expands_past_the_cap_is_stopped_even_if_its_header_lies(
    good: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "_directory_problems", lambda zf: None)  # header checks skipped
        m.setattr(bundle_mod, "MAX_MEMBER_BYTES", 10)
        assert code_of(good) == "BUNDLE_TOO_LARGE"


def test_a_highly_compressible_member_is_bounded_by_the_decompressed_cap(
    good: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    bomb = archive({**members(good), "receipts.json": b"[" + b"0," * 200_000 + b"0]"})
    assert len(bomb) < 5_000  # tiny on disk
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "MAX_MEMBER_BYTES", 100_000)
        assert code_of(bomb) == "BUNDLE_TOO_LARGE"


# --- damage and tampering ---------------------------------------------------------------------


def test_not_an_archive_or_truncated_is_corrupt(good: bytes) -> None:
    assert code_of(b"") == "BUNDLE_CORRUPT"
    assert code_of(b"not a zip at all") == "BUNDLE_CORRUPT"
    assert code_of(good[: len(good) // 2]) == "BUNDLE_CORRUPT"


def test_any_single_byte_damage_is_either_refused_cleanly_or_changes_the_address(
    good: bytes,
) -> None:
    for pos in range(0, len(good), 1):
        damaged = bytearray(good)
        damaged[pos] ^= 0xFF
        try:
            b = read_bundle(bytes(damaged))
        except BundleError:
            continue  # refused with a stable code, never an unexpected exception
        assert b.bundle_sha256 != hashlib.sha256(good).hexdigest()


def test_a_changed_member_with_an_unchanged_index_fails_its_checksum(good: bytes) -> None:
    files = members(good)
    files["events.ndjson"] += b" "
    assert code_of(archive(files)) == "BUNDLE_CHECKSUM_MISMATCH"


def test_files_missing_from_or_absent_from_the_index_are_refused(good: bytes) -> None:
    files = members(good)
    del files["receipts.json"]  # listed in the index but gone
    assert code_of(archive(files)) == "BUNDLE_CHECKSUM_MISMATCH"
    for name in ("manifest.json", "events.ndjson", "calls.ndjson", "index.json"):
        files = members(good)
        del files[name]
        assert code_of(archive(files)) == "BUNDLE_MISSING_FILE"


def test_a_rewritten_event_with_a_rewritten_index_still_fails_the_chain(good: bytes) -> None:
    def edit(files: dict[str, bytes]) -> None:
        files["events.ndjson"] = files["events.ndjson"].replace(b'"note":"read"', b'"note":"EVIL"')

    assert code_of(reindexed(good, edit)) == "BUNDLE_EVENT_CHAIN"


def test_dropped_events_fail_the_index_head_even_when_the_chain_prefix_is_valid(
    good: bytes,
) -> None:
    def edit(files: dict[str, bytes]) -> None:
        files["events.ndjson"] = b"".join(files["events.ndjson"].splitlines(keepends=True)[:-1])

    assert code_of(reindexed(good, edit)) == "BUNDLE_EVENT_CHAIN"


def test_a_changed_manifest_does_not_match_its_recorded_hash(good: bytes) -> None:
    def edit(files: dict[str, bytes]) -> None:
        doc = json.loads(files["manifest.json"])
        doc["seed"] = "43"
        files["manifest.json"] = json.dumps(doc).encode()

    assert code_of(reindexed(good, edit)) == "BUNDLE_MANIFEST_MISMATCH"


@pytest.mark.parametrize(
    ("member", "body", "code"),
    [
        ("manifest.json", b'{"manifest_version": "1.0"}', "BUNDLE_INVALID"),
        ("manifest.json", b"NaN", "BUNDLE_INVALID"),
        ("manifest.json", b"\xff\xfe", "BUNDLE_INVALID"),
        ("events.ndjson", b'{"seq": 1}\n', "BUNDLE_INVALID"),
        ("events.ndjson", b'{"seq": 1}', "BUNDLE_INVALID"),
        ("events.ndjson", b"[1]\n", "BUNDLE_INVALID"),
        ("calls.ndjson", b'{"call_id": "x"}\n', "BUNDLE_INVALID"),
        ("evaluations.json", b"{}", "BUNDLE_INVALID"),
        ("snapshots.json", b"[]", "BUNDLE_INVALID"),
        ("receipts.json", b"{}", "BUNDLE_INVALID"),
    ],
)
def test_well_checksummed_but_malformed_content_is_invalid(
    good: bytes, member: str, body: bytes, code: str
) -> None:
    assert code_of(reindexed(good, lambda f: f.__setitem__(member, body))) == code


def test_an_unknown_event_type_and_duplicate_call_ids_are_invalid(good: bytes) -> None:
    def retype(files: dict[str, bytes]) -> None:
        files["events.ndjson"] = files["events.ndjson"].replace(b"agent.step", b"os.system")

    def duplicate(files: dict[str, bytes]) -> None:
        files["calls.ndjson"] = files["calls.ndjson"] * 2

    assert code_of(reindexed(good, retype)) == "BUNDLE_INVALID"
    assert code_of(reindexed(good, duplicate)) == "BUNDLE_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "something-else"),
        ("bundle_version", "2"),
        ("canonicalization_version", "acl-c14n-v9"),
    ],
)
def test_unsupported_formats_and_versions_are_refused(good: bytes, field: str, value: str) -> None:
    def edit(files: dict[str, bytes]) -> None:
        index = json.loads(files["index.json"])
        index[field] = value
        files["index.json"] = json.dumps(index).encode()

    files = members(good)
    edit(files)
    assert code_of(archive(files)) == "BUNDLE_VERSION_UNSUPPORTED"


# --- opening from a path ----------------------------------------------------------------------


def test_a_path_must_be_a_regular_file_within_the_size_cap(
    good: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert code_of(tmp_path) == "BUNDLE_UNSAFE_PATH"
    assert code_of(tmp_path / "missing") == "BUNDLE_UNSAFE_PATH"
    real = write_bundle(tmp_path, good)
    link = tmp_path / "link.acbundle"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    assert code_of(link) == "BUNDLE_UNSAFE_PATH"
    with monkeypatch.context() as m:
        m.setattr(bundle_mod, "MAX_ARCHIVE_BYTES", 10)
        assert code_of(real) == "BUNDLE_TOO_LARGE"


def test_a_same_size_edit_is_caught_by_the_content_checksum(good: bytes) -> None:
    files = members(good)
    files["events.ndjson"] = files["events.ndjson"].replace(b'"note":"read"', b'"note":"evil"')
    assert len(files["events.ndjson"]) == len(members(good)["events.ndjson"])
    assert code_of(archive(files)) == "BUNDLE_CHECKSUM_MISMATCH"


def test_a_forged_event_hash_fails_the_chain_even_though_the_head_is_recomputed(
    good: bytes,
) -> None:
    def edit(files: dict[str, bytes]) -> None:
        rows = [json.loads(line) for line in files["events.ndjson"].splitlines()]
        rows[1]["event_hash"] = "0" * 64
        files["events.ndjson"] = b"".join(json.dumps(r).encode() + b"\n" for r in rows)

    assert code_of(reindexed(good, edit)) == "BUNDLE_EVENT_CHAIN"


def test_members_are_read_with_a_bound_never_whole(
    good: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int | None] = []
    real = zipfile.ZipExtFile.read

    def spy(self: zipfile.ZipExtFile, n: int | None = -1) -> bytes:
        sizes.append(n)
        return real(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
    read_bundle(good)
    assert sizes and all(n == bundle_mod.MAX_MEMBER_BYTES + 1 for n in sizes)
