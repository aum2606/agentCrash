"""Read-only playback of an opened bundle: a structured report and a terminal rendering.

Playback shows recorded evidence; it never runs an agent or a tool (that is strict replay). Bundle
content is untrusted data, so every string that reaches a terminal has its control characters
escaped: a recorded note cannot move the cursor, recolor the screen or smuggle an escape
sequence.
"""

from typing import Any

from agentcrash.replay.bundle import Bundle

_BRIEF_KEYS = ("tool", "status", "note", "code", "stage", "effect", "rule_id", "kind", "summary")
_WIDTH = 80


def sanitize(text: str) -> str:
    """Escape C0/C1 control characters (including ESC and newlines) and DEL."""
    return "".join(
        c
        if c.isprintable() or c == " "
        else f"\\x{ord(c):02x}"
        if ord(c) < 256
        else f"\\u{ord(c):04x}"
        for c in text
    )


def _brief(payload: dict[str, Any]) -> str:
    parts = []
    for key in _BRIEF_KEYS:
        value = payload.get(key)
        if isinstance(value, str | int | bool) and not isinstance(value, float):
            parts.append(f"{key}={value}")
    return " ".join(parts)[:_WIDTH]


def playback_report(bundle: Bundle) -> dict[str, Any]:
    m = bundle.manifest
    tools = {e.call_id: e.payload.get("tool") for e in bundle.events if e.type == "tool.requested"}
    return {
        "bundle_sha256": bundle.bundle_sha256,
        "run": {
            "scenario": {"id": m.scenario.id, "version": m.scenario.version},
            "agent": {"id": m.agent.id, "version": m.agent.version},
            "seed": m.seed,
            "replay_capability": m.replay_capability,
            "source": m.source.model_dump(mode="json"),
            "toolchain": m.toolchain.model_dump(mode="json"),
        },
        "events": [
            {
                "seq": e.seq,
                "type": e.type,
                "logical_time_ms": e.logical_time_ms,
                "actor_id": e.actor_id,
                "call_id": e.call_id,
                "brief": _brief(e.payload),
            }
            for e in bundle.events
        ],
        "calls": [
            {
                "call_id": c.call_id,
                "tool": tools.get(c.call_id),
                "outcome": c.outcome.get("kind"),
                "status": c.outcome.get("status"),
            }
            for c in bundle.calls
        ],
        "evaluations": [
            {k: ev.get(k) for k in ("verdict", "primary_reason", "grader_set_hash")}
            for ev in bundle.evaluations
        ],
    }


def render_text(report: dict[str, Any]) -> list[str]:
    run = report["run"]
    lines = [
        f"bundle    {report['bundle_sha256']}",
        f"scenario  {run['scenario']['id']} v{run['scenario']['version']}",
        f"agent     {run['agent']['id']} {run['agent']['version']}  seed {run['seed']}",
        f"replay    {run['replay_capability']} capture",
        f"source    {(run['source']['commit'] or 'no commit')[:12]}"
        f"{' dirty' if run['source']['dirty'] else ' clean'}"
        f"{' patch ' + run['source']['patch_hash'][:12] if run['source']['patch_hash'] else ''}"
        f"  reproducible {'yes' if run['source']['reproducible'] else 'no'}",
        f"toolchain python {run['toolchain']['python']}"
        f"  image {run['toolchain']['image_digest'] or '-'}  {run['toolchain']['platform'] or ''}",
        f"events    {len(report['events'])}  calls {len(report['calls'])}",
    ]
    for ev in report["evaluations"]:
        lines.append(f"verdict   {ev['verdict']}  reason {ev['primary_reason'] or '-'}")
    lines.append("")
    for e in report["events"]:
        who = e["call_id"] or e["actor_id"] or ""
        lines.append(
            f"{e['seq']:>4}  t={e['logical_time_ms']:>6}  {e['type']:<22} {who:<10} {e['brief']}"
        )
    if report["calls"]:
        lines.append("")
        for c in report["calls"]:
            lines.append(f"call {c['call_id']:<10} {c['tool']}  {c['outcome']} {c['status'] or ''}")
    return [sanitize(line) for line in lines]
