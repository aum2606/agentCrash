"""Export bundle: build, content-address and safely read one attempt's evidence
(ENGINEERING_PLAN.md section 5, ADR-012).

A bundle is a deterministic ZIP: fixed member order, timestamps and permissions, so identical
evidence always yields identical bytes and therefore the same SHA-256 bundle id. The reader never
extracts to disk: it validates the archive's directory first (allowlisted flat names, no symlinks,
directories, encryption or exotic compression, size caps), then reads each member in memory with a
bounded read and checks it against the checksum index. Parsing is JSON only; nothing in a bundle is
imported, executed or installed. Beyond checksums, the reader re-verifies the event hash chain and
the manifest hash, because a checksum index can be rewritten together with the file it covers.
"""

import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from agentcrash.domain.canonical import CANONICALIZATION_VERSION, canonical_hash
from agentcrash.domain.events import EVENT_TYPES, StoredEvent, chain_problem, observable_digest
from agentcrash.domain.models import Manifest
from agentcrash.domain.runtime import StoredCall

FORMAT: Final = "agentcrash-bundle"
BUNDLE_VERSION: Final = "1"
INDEX: Final = "index.json"
MANIFEST: Final = "manifest.json"
EVENTS: Final = "events.ndjson"
CALLS: Final = "calls.ndjson"
EVALUATIONS: Final = "evaluations.json"
SNAPSHOTS: Final = "snapshots.json"
RECEIPTS: Final = "receipts.json"
REQUIRED: Final = (INDEX, MANIFEST, EVENTS, CALLS)
ALLOWED: Final = frozenset((*REQUIRED, EVALUATIONS, SNAPSHOTS, RECEIPTS))

MAX_ARCHIVE_BYTES: Final = 32 * 1024 * 1024  # compressed file on disk
MAX_MEMBER_BYTES: Final = 16 * 1024 * 1024  # one decompressed member (evidence cap is 10 MiB)
MAX_TOTAL_BYTES: Final = 48 * 1024 * 1024  # all members decompressed
_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


class BundleError(Exception):
    """The bundle cannot be built or is not safe/valid to open. ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Bundle:
    bundle_sha256: str  # content address: SHA-256 of the archive bytes
    manifest: Manifest
    events: list[StoredEvent]
    calls: list[StoredCall]
    evaluations: list[dict[str, Any]]
    snapshots: dict[str, Any]
    receipts: list[dict[str, Any]]
    index: dict[str, Any]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _ndjson(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_json(r) + b"\n" for r in rows)


def _event_row(e: StoredEvent) -> dict[str, Any]:
    return {
        "seq": e.seq,
        "type": e.type,
        "logical_time_ms": e.logical_time_ms,
        "actor_id": e.actor_id,
        "call_id": e.call_id,
        "payload": e.payload,
        "wall_time": e.wall_time.isoformat(),
        "prev_hash": e.prev_hash,
        "event_hash": e.event_hash,
    }


def _call_row(c: StoredCall) -> dict[str, Any]:
    return {
        "call_id": c.call_id,
        "call_index": c.call_index,
        "tool_ordinal": c.tool_ordinal,
        "input_hash": c.input_hash,
        "outcome": c.outcome,
    }


# --- build ----------------------------------------------------------------------------------


def build_bundle(
    manifest: Manifest,
    events: list[StoredEvent],
    calls: list[StoredCall],
    evaluations: list[dict[str, Any]] | None = None,
    snapshots: dict[str, Any] | None = None,
    receipts: list[dict[str, Any]] | None = None,
) -> bytes:
    """Deterministic archive bytes for one attempt. Refuses evidence that would not read back."""
    problem = chain_problem(events)
    if problem is not None:
        raise BundleError("BUNDLE_EVENT_CHAIN", f"refusing to export a broken chain: {problem}")
    members: dict[str, bytes] = {
        MANIFEST: _json(manifest.model_dump(mode="json")),
        EVENTS: _ndjson([_event_row(e) for e in events]),
        CALLS: _ndjson([_call_row(c) for c in calls]),
        EVALUATIONS: _json(evaluations or []),
        SNAPSHOTS: _json(snapshots or {}),
        RECEIPTS: _json(receipts or []),
    }
    index = {
        "format": FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "manifest_hash": canonical_hash(manifest.model_dump(mode="json")),
        "event_count": len(events),
        "event_head": observable_digest(events),
        "files": {n: {"sha256": _sha(b), "size": len(b)} for n, b in members.items()},
    }
    members[INDEX] = _json(index)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, members[name])
    data = out.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BundleError("BUNDLE_TOO_LARGE", f"{len(data)} bytes exceeds {MAX_ARCHIVE_BYTES}")
    return data


def write_bundle(directory: Path, data: bytes) -> Path:
    """Publish ``data`` as ``<sha256>.acbundle`` (temp file, fsync, rename). Idempotent."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_sha(data)}.acbundle"
    if target.exists():
        return target
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


# --- safe read ------------------------------------------------------------------------------


def _directory_problems(zf: zipfile.ZipFile) -> None:
    seen: set[str] = set()
    total = 0
    for info in zf.infolist():
        name = info.filename
        if name in seen:
            raise BundleError("BUNDLE_DUPLICATE_ENTRY", name)
        seen.add(name)
        # Names are checked before anything else: no separators, drives, dots or empty parts.
        if (
            not name
            or "/" in name
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or name.startswith(".")
        ):
            raise BundleError("BUNDLE_UNSAFE_PATH", repr(name))
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BundleError("BUNDLE_SYMLINK", name)
        if info.is_dir() or (info.external_attr >> 16) & 0o170000 not in (0, stat.S_IFREG):
            raise BundleError("BUNDLE_UNSUPPORTED_ENTRY", f"{name} is not a regular file")
        if name not in ALLOWED:
            raise BundleError("BUNDLE_UNKNOWN_FILE", name)
        if info.flag_bits & 0x1:
            raise BundleError("BUNDLE_UNSUPPORTED_ENTRY", f"{name} is encrypted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise BundleError("BUNDLE_UNSUPPORTED_ENTRY", f"{name} uses an unsupported method")
        if info.file_size > MAX_MEMBER_BYTES:
            raise BundleError("BUNDLE_TOO_LARGE", f"{name} declares {info.file_size} bytes")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise BundleError("BUNDLE_TOO_LARGE", "members exceed the total size cap")
    missing = [n for n in REQUIRED if n not in seen]
    if missing:
        raise BundleError("BUNDLE_MISSING_FILE", ", ".join(missing))


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        with zf.open(name) as handle:
            data = handle.read(MAX_MEMBER_BYTES + 1)  # never trust the declared size
    except Exception as err:  # noqa: BLE001 - zipfile raises many types on hostile bytes
        raise BundleError("BUNDLE_CORRUPT", f"{name}: {err!r}") from err
    if len(data) > MAX_MEMBER_BYTES:
        raise BundleError("BUNDLE_TOO_LARGE", f"{name} expands past {MAX_MEMBER_BYTES} bytes")
    return data


def _no_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token}")


def _parse(data: bytes, name: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError) as err:
        raise BundleError("BUNDLE_INVALID", f"{name} is not valid JSON: {err}") from err


def _rows(data: bytes, name: str) -> list[dict[str, Any]]:
    if data and not data.endswith(b"\n"):
        raise BundleError("BUNDLE_INVALID", f"{name} does not end with a newline")
    rows = [_parse(line, name) for line in data.splitlines()]
    if not all(isinstance(r, dict) for r in rows):
        raise BundleError("BUNDLE_INVALID", f"{name} must hold one JSON object per line")
    return rows


def _event(row: dict[str, Any]) -> StoredEvent:
    if row.get("type") not in EVENT_TYPES:
        raise BundleError("BUNDLE_INVALID", f"unknown event type {row.get('type')!r}")
    try:
        return StoredEvent(
            seq=int(row["seq"]),
            type=str(row["type"]),
            logical_time_ms=int(row["logical_time_ms"]),
            actor_id=row["actor_id"],
            call_id=row["call_id"],
            payload=dict(row["payload"]),
            wall_time=datetime.fromisoformat(row["wall_time"]),
            prev_hash=str(row["prev_hash"]),
            event_hash=str(row["event_hash"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise BundleError("BUNDLE_INVALID", f"malformed event: {err!r}") from err


def _call(row: dict[str, Any]) -> StoredCall:
    try:
        return StoredCall(
            call_id=str(row["call_id"]),
            call_index=int(row["call_index"]),
            tool_ordinal=int(row["tool_ordinal"]),
            input_hash=str(row["input_hash"]),
            outcome=dict(row["outcome"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise BundleError("BUNDLE_INVALID", f"malformed call: {err!r}") from err


def _typed(value: Any, kind: type, name: str) -> Any:
    if not isinstance(value, kind):
        raise BundleError("BUNDLE_INVALID", f"{name} must be a {kind.__name__}")
    return value


def read_bundle(source: bytes | Path) -> Bundle:
    """Open a bundle read-only. Raises ``BundleError`` with a stable code on any problem."""
    if isinstance(source, Path):
        if source.is_symlink() or not source.is_file():
            raise BundleError("BUNDLE_UNSAFE_PATH", "the bundle path is not a regular file")
        if source.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BundleError("BUNDLE_TOO_LARGE", f"{source.stat().st_size} bytes on disk")
        source = source.read_bytes()
    if len(source) > MAX_ARCHIVE_BYTES:
        raise BundleError("BUNDLE_TOO_LARGE", f"{len(source)} bytes exceeds {MAX_ARCHIVE_BYTES}")
    try:
        zf = zipfile.ZipFile(io.BytesIO(source))
    except Exception as err:  # noqa: BLE001 - zipfile raises many types on hostile bytes
        raise BundleError("BUNDLE_CORRUPT", f"not a readable archive: {err!r}") from err
    with zf:
        _directory_problems(zf)
        raw = {info.filename: _read_member(zf, info.filename) for info in zf.infolist()}

    index = _typed(_parse(raw[INDEX], INDEX), dict, INDEX)
    if index.get("format") != FORMAT:
        raise BundleError("BUNDLE_VERSION_UNSUPPORTED", f"format {index.get('format')!r}")
    if index.get("bundle_version") != BUNDLE_VERSION:
        raise BundleError("BUNDLE_VERSION_UNSUPPORTED", f"version {index.get('bundle_version')!r}")
    if index.get("canonicalization_version") != CANONICALIZATION_VERSION:
        raise BundleError(
            "BUNDLE_VERSION_UNSUPPORTED",
            f"canonicalization {index.get('canonicalization_version')!r}",
        )
    listed = _typed(index.get("files"), dict, "index.files")
    if set(listed) != set(raw) - {INDEX}:
        raise BundleError(
            "BUNDLE_CHECKSUM_MISMATCH", "the index and the archive list different files"
        )
    for name, entry in listed.items():
        if (
            not isinstance(entry, dict)
            or entry.get("sha256") != _sha(raw[name])
            or entry.get("size") != len(raw[name])
        ):
            raise BundleError("BUNDLE_CHECKSUM_MISMATCH", name)

    manifest_doc = _parse(raw[MANIFEST], MANIFEST)
    try:
        manifest = Manifest.model_validate(manifest_doc)
    except ValidationError as err:
        raise BundleError("BUNDLE_INVALID", f"manifest: {err.error_count()} errors") from err
    # Hash the document as written, not the re-dumped model, so a bundle exported before a
    # manifest gained optional fields still verifies.
    if canonical_hash(manifest_doc) != index.get("manifest_hash"):
        raise BundleError("BUNDLE_MANIFEST_MISMATCH", "manifest does not match its recorded hash")

    events = [_event(r) for r in _rows(raw[EVENTS], EVENTS)]
    problem = chain_problem(events)
    if problem is not None:
        raise BundleError("BUNDLE_EVENT_CHAIN", problem)
    if index.get("event_count") != len(events) or index.get("event_head") != observable_digest(
        events
    ):
        raise BundleError("BUNDLE_EVENT_CHAIN", "events do not match the index head")
    calls = [_call(r) for r in _rows(raw[CALLS], CALLS)]
    if len({c.call_id for c in calls}) != len(calls):
        raise BundleError("BUNDLE_INVALID", "duplicate call ids")

    return Bundle(
        bundle_sha256=_sha(source),
        manifest=manifest,
        events=events,
        calls=calls,
        evaluations=_typed(_parse(raw.get(EVALUATIONS, b"[]"), EVALUATIONS), list, EVALUATIONS),
        snapshots=_typed(_parse(raw.get(SNAPSHOTS, b"{}"), SNAPSHOTS), dict, SNAPSHOTS),
        receipts=_typed(_parse(raw.get(RECEIPTS, b"[]"), RECEIPTS), list, RECEIPTS),
        index=index,
    )
