"""Object-key JSON Pointer mutation: pure, copy-on-write, no array or root edits."""

import copy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcrash.domain.pointer import PointerError, check_shape, mutate, segments

BODY: dict[str, Any] = {"a": 1, "b": {"c": 2, "d/e": 3, "f~g": 4}, "list": [1, 2]}


def test_remove_and_rename_do_not_touch_the_input() -> None:
    before = copy.deepcopy(BODY)
    assert mutate(BODY, "/a", "remove", None) == {"b": BODY["b"], "list": [1, 2]}
    assert mutate(BODY, "/b/c", "rename", "z")["b"] == {"z": 2, "d/e": 3, "f~g": 4}
    assert before == BODY


def test_rename_keeps_key_order() -> None:
    assert list(mutate(BODY, "/a", "rename", "z")) == ["z", "b", "list"]


def test_escaped_segments() -> None:
    assert segments("/b/d~1e") == ["b", "d/e"] and segments("/f~0g") == ["f~g"]
    assert mutate(BODY, "/b/d~1e", "remove", None)["b"] == {"c": 2, "f~g": 4}


@pytest.mark.parametrize(
    ("pointer", "operation", "new_key"),
    [
        ("/missing", "remove", None),
        ("/a/x", "remove", None),  # parent is a leaf
        ("/list/0", "remove", None),  # arrays are not addressable
        ("/b/c", "rename", "d/e"),  # destination exists
    ],
)
def test_unresolvable_pointers_raise_instead_of_doing_nothing(
    pointer: str, operation: str, new_key: str | None
) -> None:
    with pytest.raises(PointerError):
        mutate(BODY, pointer, operation, new_key)


def test_check_shape_agrees_with_runtime_resolution() -> None:
    shape = {"a": None, "b": {"c": None}}
    assert check_shape(shape, "/b/c", "remove", None) is None
    assert check_shape(shape, "/b/x", "remove", None) == "POINTER_NOT_FOUND"
    assert check_shape(shape, "/a", "rename", "b") == "RENAME_TARGET_EXISTS"


@given(st.dictionaries(st.sampled_from("abcdef"), st.integers(), min_size=1))
def test_remove_drops_exactly_one_key(body: dict[str, int]) -> None:
    key = sorted(body)[0]
    out = mutate(body, f"/{key}", "remove", None)
    assert out == {k: v for k, v in body.items() if k != key}
