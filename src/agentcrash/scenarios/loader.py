"""Safe scenario YAML loading (ENGINEERING_PLAN section 4, ADR-005, ADR-009).

Scenario text is untrusted data. This loader never constructs Python objects: it rejects
explicit tags, anchors/aliases (alias bombs), merge keys, multiple documents, duplicate or
non-string keys, oversized or deeply nested input and floats, before any model validation.
Errors carry stable codes and never echo document content.
"""

import json
from typing import Any

import yaml

from agentcrash.domain.diagnostics import Diagnostic, ScenarioValidationError, ValidationFailed

MAX_BYTES = 1 << 20  # 1 MiB, same as the HTTP JSON body limit
MAX_DEPTH = 32
MAX_NODES = 20_000


class _Reject(Exception):
    def __init__(self, code: str, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


def _fail(
    code: str, line: int | None = None, error: type[ValidationFailed] = ScenarioValidationError
) -> ValidationFailed:
    msg = f"line {line}" if line is not None else None
    return error([Diagnostic("", code, msg)])


def _prescan(text: str) -> None:
    """Event-level checks that run before any node is constructed."""
    depth = nodes = documents = 0
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            line = ev.start_mark.line + 1
            if isinstance(ev, yaml.AliasEvent):
                raise _Reject("YAML_ALIAS_FORBIDDEN", line)
            if isinstance(ev, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise _Reject("MULTIPLE_DOCUMENTS", line)
            if isinstance(ev, yaml.NodeEvent | yaml.CollectionStartEvent):
                if getattr(ev, "anchor", None) is not None:
                    raise _Reject("YAML_ANCHOR_FORBIDDEN", line)
                if getattr(ev, "tag", None) is not None:
                    raise _Reject("YAML_TAG_FORBIDDEN", line)
            if isinstance(ev, yaml.ScalarEvent | yaml.CollectionStartEvent):
                nodes += 1
                if nodes > MAX_NODES:
                    raise _Reject("DOCUMENT_TOO_COMPLEX", line)
            if isinstance(ev, yaml.CollectionStartEvent):
                depth += 1
                if depth > MAX_DEPTH:
                    raise _Reject("DOCUMENT_TOO_DEEP", line)
            elif isinstance(ev, yaml.CollectionEndEvent):
                depth -= 1
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise _Reject("YAML_SYNTAX_ERROR", mark.line + 1 if mark else None) from None


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise _Reject("YAML_SYNTAX_ERROR", node.start_mark.line + 1)
        out: dict[str, Any] = {}
        # flatten_mapping (merge keys) is deliberately not called: "<<" is just an unknown key.
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _Reject("YAML_NON_STRING_KEY", key_node.start_mark.line + 1)
            if key in out:
                raise _Reject("DUPLICATE_KEY", key_node.start_mark.line + 1)
            out[key] = self.construct_object(value_node, deep=deep)
        return out


def _has_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, list):
        return any(_has_float(v) for v in value)
    if isinstance(value, dict):
        return any(_has_float(v) for v in value.values())
    return False


def load_scenario_document(data: bytes) -> dict[str, Any]:
    """Parse one YAML (or JSON) scenario document into plain dict/list/str/int/bool/None."""
    if len(data) > MAX_BYTES:
        raise _fail("DOCUMENT_TOO_LARGE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("INVALID_ENCODING") from None
    try:
        _prescan(text)
        # Safe: _UniqueKeyLoader subclasses SafeLoader and tags were rejected in _prescan.
        doc = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506
    except _Reject as rej:
        raise _fail(rej.code, rej.line) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise _fail("YAML_SYNTAX_ERROR", mark.line + 1 if mark else None) from None
    if not isinstance(doc, dict):
        raise _fail("NOT_AN_OBJECT")
    if _has_float(doc):
        raise _fail("FLOAT_NOT_ALLOWED")
    return doc


def _within_limits(value: Any) -> str | None:
    """Iterative depth/node check (no recursion, so hostile nesting cannot crash it)."""
    stack = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            return "DOCUMENT_TOO_COMPLEX"
        if depth > MAX_DEPTH:
            return "DOCUMENT_TOO_DEEP"
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return None


def _json_too_deep(text: str) -> bool:
    """Bracket depth outside strings, so json.loads never meets hostile nesting. Relying on its
    RecursionError was platform-dependent: Linux reached the end of unclosed input first."""
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def load_json_document(
    data: bytes, error: type[ValidationFailed] = ScenarioValidationError
) -> dict[str, Any]:
    """Parse a JSON object with the same hostile-input rules as the YAML loader: size, depth
    and node limits, duplicate keys, floats and NaN/Infinity rejected. ``error`` selects the
    exception type (and so the top-level API error code) for the caller's document kind."""
    if len(data) > MAX_BYTES:
        raise _fail("DOCUMENT_TOO_LARGE", error=error)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("INVALID_ENCODING", error=error) from None
    if _json_too_deep(text):
        raise _fail("DOCUMENT_TOO_DEEP", error=error)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise _Reject("DUPLICATE_KEY")
            out[key] = value
        return out

    def no_float(_: str) -> Any:
        raise _Reject("FLOAT_NOT_ALLOWED")

    try:
        doc = json.loads(
            text, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_float
        )
    except _Reject as rej:
        raise _fail(rej.code, error=error) from None
    except RecursionError:
        raise _fail("DOCUMENT_TOO_DEEP", error=error) from None
    except ValueError:  # JSONDecodeError
        raise _fail("INVALID_JSON", error=error) from None
    if not isinstance(doc, dict):
        raise _fail("NOT_AN_OBJECT", error=error)
    if (limit := _within_limits(doc)) is not None:
        raise _fail(limit, error=error)
    return doc
