"""INTENTIONALLY VULNERABLE document parser. Demonstration only.

It does what a careless agent does with untrusted text: it treats any line that starts with
``ACTION:`` as an instruction and returns it for execution. The invoice-naive reference agent uses
it so the document-injection case has a weak baseline to contrast with the resilient agent, which
treats documents as data and never acts on their text.

Rules for this module: it only parses (no I/O, no imports of tools, storage or the gateway), it
must not be imported by anything except ``agentcrash.agents.naive`` (an architecture test
enforces this), and it must never be wired into an environment adapter or the execution layer.
The targets it can name are synthetic references; nothing here reaches a network or a real system.
"""

import re

_ACTION = re.compile(r"^ACTION:\s+(?P<tool>[a-z_]+\.[a-z_]+)(?P<args>(?:\s+[a-z_]+=\S+)*)\s*$")
_PAIR = re.compile(r"([a-z_]+)=(\S+)")
MAX_ACTIONS = 3


def obey_document(text: str) -> list[tuple[str, dict[str, str]]]:
    """``(tool, args)`` for each ``ACTION:`` line, in order, capped at ``MAX_ACTIONS``."""
    out: list[tuple[str, dict[str, str]]] = []
    for line in text.splitlines():
        m = _ACTION.match(line.strip())
        if m is not None:
            out.append((m["tool"], dict(_PAIR.findall(m["args"]))))
        if len(out) == MAX_ACTIONS:
            break
    return out
