"""The stored regression baseline: a report with everything machine- or run-specific removed.

Run ids and the experiment id are fresh each time, manifest hashes include source and toolchain
facts, the grader-set hash follows source text, and wall time depends on the machine, so none of
them is kept. What is left (verdicts, reasons, counts, exposure, logical time, usage) is
deterministic in mock mode, so two honest runs of the same suite produce the same document. A
baseline file changes only through a reviewed commit; nothing here rewrites one."""

from typing import Any

BASELINE_SCHEMA_VERSION = "1"
_DROPPED = ("experiment_id", "manifest_hashes", "grader_set_hash")


def baseline_of(report: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in report.items() if k not in _DROPPED}
    view["latency_summary"] = {
        role: {"logical_ms": lat["logical_ms"]} for role, lat in report["latency_summary"].items()
    }
    return {"baseline_schema_version": BASELINE_SCHEMA_VERSION, **view}


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Readable paths where two baselines disagree (at most one line per difference)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        return [d for k in keys for d in differences(expected.get(k), actual.get(k), f"{path}/{k}")]
    if isinstance(expected, list) and isinstance(actual, list):
        found = [
            d
            for i, (e, a) in enumerate(zip(expected, actual, strict=False))
            for d in differences(e, a, f"{path}/{i}")
        ]
        if len(expected) != len(actual):
            found.append(f"{path}: {len(expected)} items expected, {len(actual)} found")
        return found
    return [] if expected == actual else [f"{path}: expected {expected!r}, found {actual!r}"]
