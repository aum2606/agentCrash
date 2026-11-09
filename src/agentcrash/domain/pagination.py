"""Opaque keyset cursors encoding ``(created_at, id...)`` (CONTRACTS.md section 3)."""

import base64
import binascii
import json
from datetime import datetime

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class InvalidCursor(ValueError):
    pass


def encode_cursor(created_at: datetime, key: list[str]) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "k": key}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, key_parts: int) -> tuple[datetime, list[str]]:
    """Strictly parse a cursor; anything unexpected is ``InvalidCursor`` (HTTP 400)."""
    try:
        if len(cursor) > 512:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        t, k = data["t"], data["k"]
        if (
            set(data) != {"t", "k"}
            or not isinstance(t, str)
            or not isinstance(k, list)
            or len(k) != key_parts
            or not all(isinstance(p, str) for p in k)
        ):
            raise ValueError
        created_at = datetime.fromisoformat(t)
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, k
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise InvalidCursor from None


def parse_limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return int(value)
