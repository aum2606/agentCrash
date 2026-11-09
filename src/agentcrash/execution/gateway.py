"""The tool gateway: policy, dispatch, authoritative events and receipts, per attempt.

Every call runs in one fenced transaction (``AttemptGate``) so the state mutation, its receipt,
its events and the stored outcome commit together or not at all. Policy decides before anything
else touches state. Agent text, tool bodies and fixtures are data: nothing here evaluates them.
Faults arrive in M3; until then a call has no fault stage and no ``fault.decided`` events.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentcrash.domain.canonical import CanonicalizationError, canonical_bytes, canonical_hash
from agentcrash.domain.events import EventTooLarge, PendingEvent
from agentcrash.domain.models import Budgets
from agentcrash.domain.redaction import redact
from agentcrash.domain.registry import ToolSpec
from agentcrash.domain.runtime import (
    MAX_ARGS_BYTES,
    AgentResult,
    AttemptGate,
    AttemptTx,
    ToolMeta,
    ToolRequest,
    ToolResponse,
)
from agentcrash.environments.invoice import tools as invoice_tools

NOTE_LIMIT = 200


class GatewayRefusal(Exception):
    """Infrastructure/control refusal, never a synthetic tool failure (CONTRACTS.md section 2)."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class AttemptPolicy:
    actor_id: str
    allowed: frozenset[str]
    forbidden: frozenset[str]
    tools: Mapping[str, ToolSpec]
    budgets: Budgets


@dataclass
class Hooks:
    """Test seams, called outside any transaction. Production code passes none."""

    before_call: Callable[[ToolRequest], None] | None = None
    after_call: Callable[[ToolRequest, ToolResponse], None] | None = None


@dataclass
class Gateway:
    gate: AttemptGate
    policy: AttemptPolicy
    wall_clock: Callable[[], datetime]
    hooks: Hooks = field(default_factory=Hooks)
    stop_reason: str | None = None  # budget_exceeded | cancelled once the gateway refuses work
    finished: AgentResult | None = None

    # --- helpers -------------------------------------------------------------------------

    def _event(
        self,
        tx: AttemptTx,
        type_: str,
        logical_ms: int,
        payload: dict[str, Any],
        call_id: str | None = None,
        actor: str | None = None,
    ) -> None:
        try:
            tx.append(PendingEvent(type_, logical_ms, actor, call_id, payload, self.wall_clock()))
        except EventTooLarge:
            raise GatewayRefusal("EVENT_TOO_LARGE", 413) from None

    def _stop(self, reason: str, code: str) -> GatewayRefusal:
        self.stop_reason = reason
        return GatewayRefusal(code, 409)

    def _admit(self, tx: AttemptTx) -> None:
        if self.finished is not None:
            raise GatewayRefusal("ATTEMPT_FINISHED", 409)
        if tx.cancel_requested():
            raise self._stop("cancelled", "ATTEMPT_CANCELLED")

    # --- tool calls ----------------------------------------------------------------------

    def tool_call(self, request: ToolRequest) -> ToolResponse:
        if self.hooks.before_call:
            self.hooks.before_call(request)
        response = self._tool_call(request)
        if self.hooks.after_call:
            self.hooks.after_call(request, response)
        return response

    def _tool_call(self, request: ToolRequest) -> ToolResponse:
        if request.actor_id != self.policy.actor_id:
            raise GatewayRefusal("ACTOR_MISMATCH", 403)
        try:
            encoded = canonical_bytes(request.args)
            input_hash = canonical_hash(
                {"tool": request.tool, "args": request.args, "key": request.idempotency_key}
            )
        except CanonicalizationError:
            raise GatewayRefusal("INVALID_REQUEST", 422) from None
        if len(encoded) > MAX_ARGS_BYTES:
            raise GatewayRefusal("ARGUMENTS_TOO_LARGE", 413)

        with self.gate.transaction() as tx:
            self._admit(tx)
            prior = tx.get_call(request.call_id)
            if prior is not None:  # transport retransmit: same recorded outcome, no new ordinal
                if prior.input_hash != input_hash:
                    raise GatewayRefusal("CALL_ID_CONFLICT", 409)
                return ToolResponse.model_validate(prior.outcome)

            counters = tx.counters()
            if counters.calls >= self.policy.budgets.tool_calls:
                raise self._stop("budget_exceeded", "BUDGET_EXCEEDED")
            call_index = counters.calls + 1
            key = (request.actor_id, request.tool)
            ordinal = counters.ordinals.get(key, 0) + 1
            logical = counters.logical_time_ms

            args_view = redact(request.args)
            self._event(
                tx,
                "tool.requested",
                logical,
                {
                    "tool": request.tool,
                    "args": args_view,
                    "args_hash": canonical_hash(args_view),
                    "idempotency_key": redact(request.idempotency_key),
                    "call_index": call_index,
                    "tool_ordinal": ordinal,
                },
                request.call_id,
                request.actor_id,
            )

            denial = self._policy_denial(request.tool)
            decision: dict[str, Any] = {
                "tool": request.tool,
                "decision": "deny" if denial else "allow",
            }
            if denial:
                decision["reason"] = denial[0]
                spec = self.policy.tools.get(request.tool)
                decision["unsafe_attempt"] = bool(
                    spec and spec.mutating and denial[0] == "forbidden"
                )
            self._event(tx, "policy.decided", logical, decision, request.call_id, request.actor_id)

            if denial:
                outcome = invoice_tools.ToolOutcome(
                    denial[1], {"error": {"code": denial[2], "reason": denial[0]}}
                )
            else:
                outcome = invoice_tools.dispatch(
                    request.tool, tx.state, request.args, request.idempotency_key
                )
            body = redact(outcome.body)
            if outcome.mutation is not None:
                self._event(
                    tx,
                    "environment.mutated",
                    logical,
                    outcome.mutation,
                    request.call_id,
                    request.actor_id,
                )
            self._event(
                tx,
                "tool.completed",
                logical,
                {
                    "tool": request.tool,
                    "status": outcome.status,
                    "body_ref": f"sha256:{canonical_hash(body)}",
                    "source_version": outcome.source_version,
                },
                request.call_id,
                request.actor_id,
            )
            response = ToolResponse(
                status=outcome.status,
                body=body,
                meta=ToolMeta(
                    call_index=call_index,
                    tool_ordinal=ordinal,
                    logical_time_ms=logical,
                    source_version=outcome.source_version,
                ),
            )
            tx.record_call(
                request.call_id,
                call_index,
                ordinal,
                request.actor_id,
                request.tool,
                input_hash,
                response.model_dump(mode="json"),
            )
        return response

    def _policy_denial(self, tool: str) -> tuple[str, int, str] | None:
        """``(reason, http_status, code)`` or None. Runs before fault logic so no rule can
        grant what policy refuses."""
        spec = self.policy.tools.get(tool)
        if spec is None:
            return ("unknown_tool", 404, "UNKNOWN_TOOL")
        if tool in self.policy.forbidden or spec.deployment_forbidden:
            return ("forbidden", 403, "TOOL_FORBIDDEN")
        if tool not in self.policy.allowed:
            return ("not_allowed", 403, "TOOL_NOT_ALLOWED")
        return None

    # --- clock, steps, finish ------------------------------------------------------------

    def advance_clock(self, duration_ms: int) -> int:
        if not 0 <= duration_ms <= self.policy.budgets.logical_ms:
            raise GatewayRefusal("INVALID_REQUEST", 422)
        with self.gate.transaction() as tx:
            self._admit(tx)
            now = tx.counters().logical_time_ms + duration_ms
            if now > self.policy.budgets.logical_ms:
                raise self._stop("budget_exceeded", "BUDGET_EXCEEDED")
            self._event(tx, "clock.advanced", now, {"duration_ms": duration_ms, "to_ms": now})
        return now

    def agent_step(self, note: str) -> None:
        with self.gate.transaction() as tx:
            self._admit(tx)
            counters = tx.counters()
            if counters.steps >= self.policy.budgets.steps:
                raise self._stop("budget_exceeded", "BUDGET_EXCEEDED")
            self._event(
                tx,
                "agent.step",
                counters.logical_time_ms,
                {"step": counters.steps + 1, "note": redact(note[:NOTE_LIMIT])},
                None,
                self.policy.actor_id,
            )

    def agent_finish(self, result: AgentResult) -> None:
        with self.gate.transaction() as tx:
            if self.finished is not None:
                return  # idempotent
            self._admit(tx)
            self._event(
                tx,
                "agent.finished",
                tx.counters().logical_time_ms,
                {"status": result.status, "summary": redact(result.summary)},
                None,
                self.policy.actor_id,
            )
        self.finished = result
