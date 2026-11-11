"""Pure, deterministic fault matching (ENGINEERING_PLAN.md section 4, algorithm steps 1-5).

No I/O, no clock, no global PRNG: the same inputs always give the same decisions, so the same
tool occurrence meets the same fault whichever agent reaches it. The caller supplies the current
application counts and persists the result; this module never mutates anything.

Sample = first unsigned 64 bits of SHA-256 over the canonical JSON array
``[seed, scenario_hash, actor_id, tool_name, tool_ordinal, rule_id]`` divided by 2**64
(fault engine version "1"; ``seed`` is the manifest's decimal string). Probability and sample are
compared as exact fractions, never floats.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from agentcrash.domain.canonical import canonical_bytes
from agentcrash.domain.models import FaultRule, Stage

STAGE_ORDER: dict[Stage, int] = {"before_dispatch": 0, "after_commit": 1, "after_response": 2}
Decision = Literal["applied", "not_sampled", "exhausted"]


class AmbiguousComposition(Exception):
    """Two applied rules would both be terminal in one stage, or both rewrite the payload.
    The compiler rejects this statically; reaching it at runtime means a compiler bug."""


def sample_u64(
    seed: str, scenario_hash: str, actor_id: str, tool: str, ordinal: int, rule_id: str
) -> int:
    digest = hashlib.sha256(
        canonical_bytes([seed, scenario_hash, actor_id, tool, ordinal, rule_id])
    ).digest()
    return int.from_bytes(digest[:8], "big")


def sampled(sample: int, probability: str) -> bool:
    """``sample / 2**64 < probability``, exactly."""
    return Fraction(sample, 2**64) < Fraction(Decimal(probability))


@dataclass(frozen=True)
class RuleDecision:
    rule: FaultRule
    sample: int
    decision: Decision
    applications_before: int

    @property
    def applied(self) -> bool:
        return self.decision == "applied"

    @property
    def applications_after(self) -> int:
        return self.applications_before + (1 if self.applied else 0)

    def event_payload(self, tool_ordinal: int) -> dict[str, Any]:
        """Payload of the ``fault.decided`` event; all integers/strings, so it hashes stably."""
        return {
            "rule_id": self.rule.id,
            "effect": self.rule.effect.type,
            "stage": self.rule.effect.stage,
            "tool": self.rule.tool,
            "tool_ordinal": tool_ordinal,
            "sample_u64": str(self.sample),
            "probability": self.rule.probability,
            "decision": self.decision,
            "applied": self.applied,
            "applications_before": self.applications_before,
            "applications_after": self.applications_after,
        }


def _order(rule: FaultRule) -> tuple[int, int, str]:
    return (STAGE_ORDER[rule.effect.stage], rule.priority, rule.id)


def decide(
    rules: Sequence[FaultRule],
    *,
    seed: str,
    scenario_hash: str,
    actor_id: str,
    tool: str,
    tool_ordinal: int,
    applications: Mapping[str, int],
) -> list[RuleDecision]:
    """Decisions for every *eligible* rule (tool, actor and ordinal all match), in application
    order. Ineligible rules produce nothing and so can never consume an application."""
    eligible = [
        r
        for r in rules
        if r.tool == tool
        and r.actor_id == actor_id
        and (r.on_tool_ordinals is None or tool_ordinal in r.on_tool_ordinals)
    ]
    out: list[RuleDecision] = []
    for rule in sorted(eligible, key=_order):
        used = applications.get(rule.id, 0)
        sample = sample_u64(seed, scenario_hash, actor_id, tool, tool_ordinal, rule.id)
        if used >= rule.max_applications:
            verdict: Decision = "exhausted"
        elif sampled(sample, rule.probability):
            verdict = "applied"
        else:
            verdict = "not_sampled"
        out.append(RuleDecision(rule, sample, verdict, used))
    _check_composition([d.rule for d in out if d.applied])
    return out


def _check_composition(applied: Sequence[FaultRule]) -> None:
    terminal_stages = [r.effect.stage for r in applied if r.effect.terminal]
    if len(terminal_stages) != len(set(terminal_stages)):
        raise AmbiguousComposition("two terminal effects in one stage")
    if sum(1 for r in applied if r.effect.mutates_payload) > 1:
        raise AmbiguousComposition("two payload-mutating effects in one call")
