"""Object-key-only JSON Pointer operations for ``schema_mutation`` (CONTRACTS.md section 5).

No array edits, no root replacement, no wildcard: a pointer names a chain of object keys. The same
resolution runs at compile time against a tool's declared body shape and at run time against the
real body, so a rule that compiled can never silently do nothing on a successful response.
"""

import copy
from collections.abc import Mapping
from typing import Any


class PointerError(ValueError):
    """The pointer does not resolve (missing key, non-object parent) or the rename collides."""


def segments(pointer: str) -> list[str]:
    """Unescaped segments (``~1`` -> ``/``, ``~0`` -> ``~``); the model validates the syntax."""
    return [s.replace("~1", "/").replace("~0", "~") for s in pointer[1:].split("/")]


def check_shape(
    shape: Mapping[str, Any], pointer: str, operation: str, new_key: str | None
) -> str | None:
    """Reason code if ``pointer`` does not name an existing key in the declared body ``shape``
    (nested mappings for objects, ``None`` for leaves), or if a rename destination exists."""
    *parents, leaf = segments(pointer)
    node: Any = shape
    for name in parents:
        node = node.get(name) if isinstance(node, Mapping) else None
        if not isinstance(node, Mapping):
            return "POINTER_NOT_FOUND"
    if not isinstance(node, Mapping) or leaf not in node:
        return "POINTER_NOT_FOUND"
    if operation == "rename" and new_key in node:
        return "RENAME_TARGET_EXISTS"
    return None


def mutate(
    body: dict[str, Any], pointer: str, operation: str, new_key: str | None
) -> dict[str, Any]:
    """A deep copy of ``body`` with the key removed or renamed (key order is preserved)."""
    out = copy.deepcopy(body)
    *parents, leaf = segments(pointer)
    node: Any = out
    for name in parents:
        node = node.get(name) if isinstance(node, dict) else None
    if not isinstance(node, dict) or leaf not in node:
        raise PointerError(pointer)
    if operation == "remove":
        del node[leaf]
        return out
    if new_key is None or new_key in node:
        raise PointerError(pointer)
    renamed = {(new_key if k == leaf else k): v for k, v in node.items()}
    node.clear()
    node.update(renamed)
    return out
