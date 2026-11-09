import copy
import random
from pathlib import Path
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from agentcrash.domain.canonical import canonical_bytes, canonical_hash
from agentcrash.domain.registry import Registry
from agentcrash.scenarios.compiler import compile_scenario
from agentcrash.scenarios.loader import load_scenario_document

SCENARIO = Path(__file__).resolve().parents[2] / "scenarios/invoice/v1/invoice-429.yaml"

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=25,
)


def shuffled(value: Any, rng: random.Random) -> Any:
    """Same data with every object's keys re-inserted in random order."""
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: shuffled(v, rng) for k, v in items}
    if isinstance(value, list):
        return [shuffled(v, rng) for v in value]
    return value


@given(json_values, st.randoms(use_true_random=False))
def test_hash_ignores_key_order(value: Any, rng: random.Random) -> None:
    assert canonical_bytes(shuffled(value, rng)) == canonical_bytes(value)


@given(json_values)
def test_encoding_is_deterministic_and_input_is_unmodified(value: Any) -> None:
    before = copy.deepcopy(value)
    assert canonical_hash(value) == canonical_hash(value)
    assert value == before


@given(st.randoms(use_true_random=False))
def test_scenario_hash_ignores_rule_grader_and_tool_order(
    registry: Registry, rng: random.Random
) -> None:
    doc = load_scenario_document(SCENARIO.read_bytes())  # fresh copy per example
    extra = copy.deepcopy(doc["faults"][0])
    extra.update(id="po-delay", on_tool_ordinals=[2, 1], effect={"type": "delay", "duration_ms": 5})
    doc["faults"].append(extra)
    expected = compile_scenario(copy.deepcopy(doc), registry).content_hash
    rng.shuffle(doc["faults"])
    rng.shuffle(doc["graders"])
    rng.shuffle(doc["policy"]["allowed_tools"])
    assert compile_scenario(shuffled(doc, rng), registry).content_hash == expected
