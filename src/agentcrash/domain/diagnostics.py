"""Stable, bounded validation diagnostics (CONTRACTS.md section 3 error envelope ``details``).

Messages never echo submitted values, so hostile document text cannot leak into errors.
"""

import re
from dataclasses import dataclass

from pydantic import ValidationError

MAX_DIAGNOSTICS = 50
_CUSTOM_CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")

_TYPE_CODES = {
    "extra_forbidden": "UNKNOWN_FIELD",
    "missing": "MISSING_FIELD",
    "union_tag_invalid": "UNKNOWN_EFFECT_TYPE",
    "union_tag_not_found": "MISSING_EFFECT_TYPE",
    "too_long": "TOO_MANY_ITEMS",
    "string_too_long": "VALUE_TOO_LARGE",
    "string_too_short": "INVALID_VALUE",
    "string_pattern_mismatch": "INVALID_FORMAT",
}


@dataclass(frozen=True)
class Diagnostic:
    path: str  # RFC 6901 JSON Pointer into the submitted document ("" = whole document)
    code: str
    message: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"path": self.path, "code": self.code}
        if self.message:
            out["message"] = self.message
        return out


class ValidationFailed(Exception):
    """Rejected input. ``code`` is the stable top-level error code; ``details`` is bounded."""

    code = "VALIDATION_FAILED"

    def __init__(self, details: list[Diagnostic]) -> None:
        self.details = details[:MAX_DIAGNOSTICS]
        super().__init__(f"{self.code}: " + ", ".join(f"{d.code}@{d.path}" for d in self.details))


class ScenarioValidationError(ValidationFailed):
    code = "SCENARIO_VALIDATION_FAILED"


class RunValidationError(ValidationFailed):
    code = "RUN_VALIDATION_FAILED"


def _pointer(parts: list[str]) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def from_pydantic(exc: ValidationError) -> list[Diagnostic]:
    """Normalize framework errors to stable codes; no framework structure leaks out."""
    out: list[Diagnostic] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        loc: list[str] = []
        skip = False
        for part in err["loc"]:
            if skip:  # discriminated union: pydantic inserts the matched tag after "effect"
                skip = False
                continue
            loc.append(str(part))
            skip = part == "effect"
        etype = err["type"]
        if _CUSTOM_CODE.match(etype):
            code = etype
        elif etype == "literal_error" and loc and loc[-1] == "schema_version":
            code = "UNSUPPORTED_SCHEMA_VERSION"
        elif etype == "literal_error" and loc and loc[-1] == "mode":
            code = "UNSUPPORTED_MODE"
        elif etype == "less_than_equal" and loc[:1] == ["budgets"]:
            code = "BUDGET_EXCEEDS_CAP"
        else:
            code = _TYPE_CODES.get(etype, "INVALID_VALUE")
        out.append(Diagnostic(_pointer(loc), code, str(err["msg"])[:200]))
    return out
