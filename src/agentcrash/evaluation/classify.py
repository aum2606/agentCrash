"""Failure classification (ENGINEERING_PLAN.md section 6, taxonomy version 1).

Pure: reads the evidence and the grades, returns every finding plus one primary reason. A grade
that did not pass contributes its reason code when that code is in the taxonomy; two codes that
no single grader can see come from the trace instead: a suspected loop (the gateway's heuristic
flag) and an unhandled schema change (a schema mutation was applied and the agent never read the
tool again successfully). ``RETRY_EXHAUSTED`` comes from the recovery grader. A passing run has no
primary reason, but its informational findings are still reported.
"""

from agentcrash.domain.evidence import REASON_PRIORITY, Evidence, Finding, Grade

TAXONOMY = frozenset(REASON_PRIORITY)


def classify(
    evidence: Evidence, grades: list[Grade], verdict: str
) -> tuple[str | None, list[Finding]]:
    findings: list[Finding] = []
    for grade in grades:
        if grade.result != "pass" and grade.reason_code in TAXONOMY:
            findings.append(Finding(code=grade.reason_code, source=grade.grader_id))
    if any(
        "loop_suspected" in e.payload
        for e in evidence.events
        if e.type in ("tool.completed", "tool.transport_failed")
    ):
        findings.append(Finding(code="LOOP_SUSPECTED", source="events"))
    unhandled = _unhandled_schema_change(evidence) if verdict != "pass" else None
    if unhandled is not None:
        findings.append(Finding(code="SCHEMA_UNHANDLED", source="events", detail=unhandled))
    # One finding per (code, source), stable order: priority first, then source.
    unique = {(f.code, f.source): f for f in findings}
    ordered = sorted(unique.values(), key=lambda f: (_rank(f.code), f.source))
    primary = None if verdict == "pass" or not ordered else ordered[0].code
    return primary, ordered


def _rank(code: str) -> int:
    return REASON_PRIORITY.index(code) if code in REASON_PRIORITY else len(REASON_PRIORITY)


def _unhandled_schema_change(evidence: Evidence) -> str | None:
    """The tool whose mutated response the agent never recovered from, or None."""
    for event in evidence.events:
        mutation = event.payload.get("mutation") if event.type == "tool.completed" else None
        if not (isinstance(mutation, dict) and mutation.get("applied")):
            continue
        tool = event.payload.get("tool")
        recovered = any(
            later.type == "tool.completed"
            and later.payload.get("tool") == tool
            and later.payload.get("status") == 200
            and "mutation" not in later.payload
            for later in evidence.events
            if later.seq > event.seq
        )
        if not recovered:
            return str(tool)
    return None
