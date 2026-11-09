"""Canonical encoding ``acl-c14n-v1`` and stable hashing (CONTRACTS.md section 1).

Object keys are sorted recursively (by code point), array order is preserved, separators are
compact, non-ASCII characters stay unescaped, Unicode is never normalized. Floats (including
NaN/Infinity) are rejected: hashed domain data uses integers and strings.
"""

import hashlib
import json
from typing import Any, Final

CANONICALIZATION_VERSION: Final = "acl-c14n-v1"
_MAX_DEPTH = 64


class CanonicalizationError(ValueError):
    """The value cannot be encoded canonically."""


def _check(value: Any, path: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise CanonicalizationError(f"{path or '/'}: nesting deeper than {_MAX_DEPTH}")
    # bool is an int subclass and is allowed; float is not.
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, list | tuple):
        for i, item in enumerate(value):
            _check(item, f"{path}/{i}", depth + 1)
    elif isinstance(value, dict):
        for k, item in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"{path or '/'}: object key is not a string")
            _check(item, f"{path}/{k}", depth + 1)
    else:
        raise CanonicalizationError(f"{path or '/'}: unsupported type {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    _check(value, "", 0)
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates cannot be UTF-8 encoded
        raise CanonicalizationError("string is not valid Unicode text") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))
