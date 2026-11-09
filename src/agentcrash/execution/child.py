"""Child runner entry point: ``python -I -m agentcrash.execution.child``.

Reads one JSON line from stdin (gateway URL, capability, agent identity, task, seed), runs the
allowlisted adapter, reports the result to the gateway and exits. It imports no storage code and
receives no database URL, secret or manifest; its environment is built by the supervisor.

Exit codes: 0 result delivered, 3 the gateway ended the attempt (cancel/budget/fence), 2 bad
input, 5 the gateway could not be reached.
"""

import asyncio
import json
import sys
from typing import Any

from agentcrash.agents.registry import ADAPTERS
from agentcrash.agents.rng import SeededRng
from agentcrash.domain.runtime import AgentResult, AgentTask, AttemptEnded
from agentcrash.execution.client import GatewayClock, GatewayError, GatewayTransport, HttpToolClient


async def _run(spec: dict[str, Any]) -> int:
    transport = GatewayTransport(spec["url"], spec["capability"])
    factory = ADAPTERS.get((spec["agent_id"], spec["agent_version"]))
    if factory is None:
        return 2
    task = AgentTask.model_validate(spec["task"])
    try:
        result = await factory().run(
            task,
            HttpToolClient(transport),
            GatewayClock(transport),
            SeededRng(task.seed, spec["agent_id"]),
        )
    except AttemptEnded:
        return 3
    except GatewayError as err:
        result = AgentResult(status="error", summary=f"gateway refused: {err.code}")
    except Exception as err:
        # Only the type name: exception text may echo untrusted tool data.
        result = AgentResult(status="error", summary=f"agent raised {type(err).__name__}")
    try:
        await transport.post("/internal/v1/agent/finish", result.model_dump(mode="json"))
    except AttemptEnded:
        return 3
    except (OSError, GatewayError):
        return 5
    return 0


def main() -> int:
    try:
        spec = json.loads(sys.stdin.readline())
    except ValueError:
        return 2
    try:
        return asyncio.run(_run(spec))
    except OSError:
        return 5


if __name__ == "__main__":
    sys.exit(main())
