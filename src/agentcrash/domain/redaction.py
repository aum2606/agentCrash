"""Redact before persistence (ENGINEERING_PLAN.md section 5; CLAUDE.md invariants).

Applied to every tool argument, tool body and event payload before it is stored. Values under
credential-like keys and strings that look like credentials are replaced; nothing is truncated
silently and the structure of the document is preserved. Redaction policy version 1.
"""

import re
from typing import Any, Final

REDACTION_VERSION: Final = "1"
REDACTED: Final = "[REDACTED]"

_SECRET_KEY = re.compile(
    r"(authorization|password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)", re.I
)
_SECRET_VALUE = re.compile(
    r"(bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.I,
)


def redact(value: Any) -> Any:
    """A redacted deep copy of JSON-like ``value``."""
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return _SECRET_VALUE.sub(REDACTED, value)
    return value
