"""Attempt supervisor: runs one claimed attempt in a child process and closes it exactly once.

The supervisor owns the lease (heartbeat), the real deadline, cancellation polling and bounded
child shutdown. The agent runs in a separate process with only a loopback URL and a per-attempt
capability; it never sees the database, the manifest or the graders. Anything the supervisor
cannot account for (child died without a result, storage failed, lease lost) is handled on the
infrastructure path: the attempt is closed ``worker_lost`` and replaced, never silently resumed.
"""

import contextlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentcrash.agents.registry import ADAPTERS
from agentcrash.domain.events import PendingEvent
from agentcrash.domain.models import Manifest
from agentcrash.domain.ports import Claim, StaleFence
from agentcrash.domain.registry import ToolSpec
from agentcrash.domain.task import task_from_manifest
from agentcrash.environments.invoice.fixture import load_bundle
from agentcrash.execution.gateway import IMPLEMENTED_EFFECTS, AttemptPolicy, Gateway, Hooks
from agentcrash.execution.server import GatewayServer
from agentcrash.storage.attempts import AttemptRepo
from agentcrash.storage.queue import JobQueue

log = logging.getLogger(__name__)

_CHILD_ENV_KEYS = ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL")
_TERMINATIONS = {
    "completed": "agent_completed",
    "gave_up": "agent_completed",
    "error": "agent_error",
}


@dataclass
class SupervisorConfig:
    heartbeat_s: float = 5.0
    cancel_poll_s: float = 1.0
    tick_s: float = 0.05
    kill_grace_s: float = 2.0
    drain_s: float = 10.0
    child_argv: Sequence[str] = (sys.executable, "-I", "-m", "agentcrash.execution.child")


@dataclass
class AttemptSupervisor:
    queue: JobQueue
    repo: AttemptRepo
    root: Path
    owner: str
    tools: dict[str, ToolSpec]
    config: SupervisorConfig = field(default_factory=SupervisorConfig)
    hooks: Hooks = field(default_factory=Hooks)
    shutdown: threading.Event = field(default_factory=threading.Event)
    wall_clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    child: "subprocess.Popen[bytes] | None" = None  # the live child, for tests and diagnostics

    # --- public --------------------------------------------------------------------------

    def run_attempt(self, claim: Claim) -> str:
        """Run ``claim`` to a closed attempt. Returns what happened, for logs and tests:
        ``closed:<termination>``, ``unsupported:<code>``, ``fenced``, ``lease_lost`` or
        ``abandoned``."""
        manifest = self.repo.manifest(claim.run_id)
        problem = self._unsupported(manifest)
        if problem is not None:
            self._finalize(claim, "agent_error", None, run_error=problem[0])
            return f"unsupported:{problem[0]}"

        bundle = load_bundle(self.root / "fixtures" / "invoice" / "v1" / "standard.json")
        gateway = Gateway(
            self.repo.gate(claim.attempt_id, claim.fencing_token),
            AttemptPolicy(
                actor_id="agent",
                allowed=frozenset(manifest.scenario.policy.allowed_tools),
                forbidden=frozenset(manifest.scenario.policy.forbidden_tools),
                tools=self.tools,
                budgets=manifest.budgets,
                faults=tuple(manifest.scenario.faults),
                seed=manifest.seed,
                scenario_hash=manifest.scenario_hash,
                fixtures=bundle.faults,
            ),
            self.wall_clock,
            self.hooks,
        )
        capability = secrets.token_urlsafe(32)
        server = GatewayServer(gateway, capability)
        port = server.start()
        try:
            self.repo.start_attempt(
                claim.attempt_id,
                claim.fencing_token,
                bundle.rows(),
                PendingEvent(
                    "attempt.started",
                    0,
                    None,
                    None,
                    {
                        "fixture_hash": manifest.fixture.hash,
                        "agent": {"id": manifest.agent.id, "version": manifest.agent.version},
                        "seed": manifest.seed,
                        "budgets": manifest.budgets.model_dump(mode="json"),
                        "ordinal": claim.ordinal,
                    },
                    self.wall_clock(),
                ),
            )
            return self._supervise(claim, manifest, gateway, server, port, capability)
        except StaleFence:
            return "fenced"
        finally:
            server.stop()

    # --- internals -----------------------------------------------------------------------

    def _unsupported(self, manifest: Manifest) -> tuple[str] | None:
        """Refuse, with a stable code, anything M2 cannot execute faithfully rather than run it
        as if it were healthy (faults arrive in M3; the resilient agent too)."""
        if (manifest.agent.id, manifest.agent.version) not in ADAPTERS:
            return ("ADAPTER_NOT_IMPLEMENTED",)
        if any(r.effect.type not in IMPLEMENTED_EFFECTS for r in manifest.scenario.faults):
            return ("FAULTS_NOT_IMPLEMENTED",)
        state = self.root / "fixtures" / "invoice" / "v1" / "standard.json"
        if load_bundle(state).hash != manifest.fixture.hash:
            return ("FIXTURE_CHANGED",)
        return None

    def _child_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _spawn(self, spec: dict[str, Any], workdir: str) -> "subprocess.Popen[bytes]":
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(self.config.child_argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._child_env(),
            cwd=workdir,
            start_new_session=sys.platform != "win32",  # own process group for killpg
        )
        if child.stdin is None:
            raise RuntimeError("child stdin is not a pipe")
        child.stdin.write((json.dumps(spec) + "\n").encode())
        child.stdin.close()
        return child

    def _signal_tree(self, child: "subprocess.Popen[bytes]", *, force: bool) -> None:
        """Signal the child *and anything it spawned*. A venv's ``python.exe`` on Windows is a
        launcher that starts the real interpreter, so killing only the launcher would orphan
        the agent process (which keeps running with its capability)."""
        if sys.platform == "win32":
            if force:
                subprocess.run(  # noqa: S603 - fixed argv, integer pid
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],  # noqa: S607
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                child.terminate()
        else:
            os.killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)

    def _stop_child(self, child: "subprocess.Popen[bytes]") -> None:
        """Ask it to exit, kill the whole tree after the grace period, always reap it."""
        try:
            if child.poll() is None:
                self._signal_tree(child, force=False)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=self.config.kill_grace_s)
            # Force even if the launcher already exited: a surviving grandchild is still ours.
            self._signal_tree(child, force=True)
        except (ProcessLookupError, PermissionError):
            pass  # the whole tree is already gone
        child.wait()

    def _supervise(
        self,
        claim: Claim,
        manifest: Manifest,
        gateway: Gateway,
        server: GatewayServer,
        port: int,
        capability: str,
    ) -> str:
        cfg = self.config
        task = task_from_manifest(manifest)
        spec = {
            "url": f"http://127.0.0.1:{port}",
            "capability": capability,
            "agent_id": manifest.agent.id,
            "agent_version": manifest.agent.version,
            "task": task.model_dump(mode="json"),
        }
        forced: str | None = None  # termination the supervisor imposes
        start = time.monotonic()
        deadline = start + manifest.budgets.wall_ms / 1000
        next_beat = start + cfg.heartbeat_s
        next_cancel = start
        drain_until: float | None = None
        with tempfile.TemporaryDirectory(prefix="agentcrash-child-") as workdir:
            child = self.child = self._spawn(spec, workdir)
            try:
                while child.poll() is None:
                    now = time.monotonic()
                    if server.fenced:
                        return "fenced"  # a newer attempt owns the run; publish nothing
                    if server.infra_error:
                        break
                    if now >= next_beat:
                        next_beat = now + cfg.heartbeat_s
                        if not self.queue.heartbeat(claim.job_id, claim.owner, claim.fencing_token):
                            return "lease_lost"
                    if self.shutdown.is_set():
                        drain_until = drain_until or now + cfg.drain_s
                        if now >= drain_until:
                            break  # drain expired: abandon for recovery
                    if forced is None and now >= deadline:
                        forced = "deadline_exceeded"
                        break
                    if forced is None and now >= next_cancel:
                        next_cancel = now + cfg.cancel_poll_s
                        if self.repo.cancel_requested(claim.run_id):
                            forced = "cancelled"
                            break
                    time.sleep(cfg.tick_s)
            finally:
                self._stop_child(child)
                self.child = None

        if server.fenced:
            return "fenced"
        reason = forced or gateway.stop_reason
        if reason is not None:
            self._finalize(claim, reason, None)
            return f"closed:{reason}"
        if gateway.finished is not None and not server.infra_error:
            result = gateway.finished
            termination = _TERMINATIONS[result.status]
            self._finalize(claim, termination, result.model_dump(mode="json"))
            return f"closed:{termination}"
        # The child ended without delivering a result, storage failed, or shutdown drained:
        # unknown territory, so recover on a fresh attempt.
        lost = self.queue.abandon(claim.job_id, claim.owner, claim.fencing_token)
        return "abandoned" if lost is not None else "lease_lost"

    def _finalize(
        self,
        claim: Claim,
        termination: str,
        result: dict[str, Any] | None,
        *,
        run_error: str | None = None,
    ) -> None:
        try:
            self.repo.finalize(
                claim.attempt_id,
                claim.fencing_token,
                termination,
                result,
                self.wall_clock(),
                run_error=run_error,
            )
        except StaleFence:
            log.warning("attempt %s was fenced out before it could close", claim.attempt_id)
