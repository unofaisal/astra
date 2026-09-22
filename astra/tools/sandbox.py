"""Runners: WHERE a process actually executes.

  * ``LocalRunner``   — runs on this host, optionally inside a
                        ``Sandbox`` wrapper (bubblewrap on Linux).
  * ``RemoteRunner``  — interface for shipping the ProcessSpec to an
                        isolated worker pool (gVisor / Firecracker /
                        a hosted sandbox).  *Interface only* — plug in your
                        own transport.

Policy model (what the sandbox enforces): read-only root filesystem,
writable = the workspace only, private /tmp, no network unless asked,
new PID/IPC/UTS namespaces, dies with its parent.  Path *prefix* checks
alone are not enough (symlinks) — that's why isolation lives in the
kernel, not in string checks.

Fail closed: ``LocalRunner(require_sandbox=True)`` refuses to run anything
when no sandbox is available instead of silently running unisolated.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from .process import ProcResult, ProcessSpec, ProcessTracker, run_process

logger = logging.getLogger(__name__)


@dataclass
class SandboxPolicy:
    network: bool = False
    writable: List[str] = field(default_factory=list)  # bind-mounted read-write
    readonly: List[str] = field(default_factory=list)  # extra read-only binds
    private_tmp: bool = True


class Sandbox(Protocol):
    name: str

    def available(self) -> bool: ...

    def wrap(self, spec: ProcessSpec, policy: SandboxPolicy) -> List[str]: ...


class BubblewrapSandbox:
    """Unprivileged namespace sandbox (the same primitive Flatpak and
    Anthropic's sandbox-runtime use on Linux)."""

    name = "bubblewrap"

    def __init__(self, executable: str = "bwrap") -> None:
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def wrap(self, spec: ProcessSpec, policy: SandboxPolicy) -> List[str]:
        exe = shutil.which(self.executable) or self.executable
        cmd: List[str] = [
            exe,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if policy.network:
            cmd.append("--share-net")
        cmd += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        if policy.private_tmp:
            cmd += ["--tmpfs", "/tmp"]
        for p in policy.readonly:
            cmd += ["--ro-bind", p, p]
        for p in policy.writable:
            cmd += ["--bind", p, p]
        if spec.cwd:
            cmd += ["--chdir", spec.cwd]
        cmd.append("--")
        return cmd + list(spec.argv)


class Runner(Protocol):
    isolated: bool

    async def run(
        self,
        spec: ProcessSpec,
        *,
        on_output: Optional[Callable[[str, bytes], Awaitable[None]]] = None,
        tracker: Optional[ProcessTracker] = None,
    ) -> ProcResult: ...


class LocalRunner:
    def __init__(self, sandbox: Optional[Sandbox] = None, *, require_sandbox: bool = False) -> None:
        self.sandbox = sandbox
        self.require_sandbox = require_sandbox

    @property
    def isolated(self) -> bool:
        return bool(self.sandbox and self.sandbox.available())

    async def run(self, spec: ProcessSpec, *, on_output=None, tracker=None) -> ProcResult:
        if self.isolated:
            policy = spec.sandbox_policy or SandboxPolicy(writable=[spec.cwd] if spec.cwd else [])
            wrapped = ProcessSpec(**{**spec.__dict__, "argv": self.sandbox.wrap(spec, policy)})  # type: ignore[union-attr]
            return await run_process(wrapped, on_output=on_output, tracker=tracker)
        if self.require_sandbox:
            return ProcResult(spawn_error="refused: no sandbox available and require_sandbox=True")
        return await run_process(spec, on_output=on_output, tracker=tracker)


class RemoteRunner:
    """Base class for runners that execute somewhere else (microVM pool,
    hosted sandbox, job queue).  Subclass and implement ``run``.

    The contract: take a ``ProcessSpec`` (argv, env, stdin, timeout, byte
    cap, sandbox policy) and return a ``ProcResult``.  Enforce the timeout
    and byte cap on the remote side; raise ``asyncio.CancelledError`` /
    cancel the remote job when the calling task is cancelled.
    """

    isolated = True

    async def run(self, spec: ProcessSpec, *, on_output=None, tracker=None) -> ProcResult:  # pragma: no cover
        raise NotImplementedError("Implement RemoteRunner.run() against your isolated worker pool.")


def default_sandbox() -> Optional[Sandbox]:
    """Best available local sandbox, or None."""
    bwrap = BubblewrapSandbox()
    return bwrap if bwrap.available() else None
