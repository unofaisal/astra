"""The ONE place subprocess code lives.

Every CLI-backed tool (manifest tools, the opt-in ``exec`` tool, custom
handlers) funnels through ``run_process``.  It provides:

  * no shell, ever — ``argv`` is a list, executed with ``exec``;
  * a minimal, explicit environment (allowlist) — the parent environment
    (API keys, DB passwords) is NOT inherited by default;
  * streaming reads with hard byte caps (memory-safe against chatty or
    hostile output), and a kill-on-overflow safety valve;
  * a wall-clock timeout, SIGTERM → grace → SIGKILL on the whole process
    *group* (so grandchildren die too), and the same cleanup when the
    calling task is cancelled (user Stop / parent cancelled);
  * optional resource limits via ``prlimit`` (when present);
  * process tracking so the runtime can kill everything on shutdown.

Stdlib only.  POSIX is fully supported; on Windows the group-kill falls
back to ``Process.kill``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from ..telemetry import metrics

logger = logging.getLogger(__name__)

DEFAULT_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")
_IS_POSIX = os.name == "posix"


@dataclass
class Limits:
    """Resource limits applied through ``prlimit`` when it is installed."""

    cpu_seconds: Optional[int] = None
    memory_bytes: Optional[int] = None
    file_size_bytes: Optional[int] = None
    max_processes: Optional[int] = None


@dataclass
class ProcessSpec:
    argv: List[str]
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None  # FINAL environment (use build_env)
    stdin: Optional[bytes] = None
    timeout: float = 60.0
    max_output_bytes: int = 1_000_000
    grace: float = 2.0
    limits: Optional[Limits] = None
    label: str = ""  # tool name, for logs/metrics
    sandbox_policy: Any = None  # astra.tools.sandbox.SandboxPolicy | None
    overflow_kill_factor: int = 10  # kill if output exceeds cap * factor


@dataclass
class ProcResult:
    returncode: Optional[int] = None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    killed: bool = False
    spawn_error: Optional[str] = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.spawn_error

    def text(self, stream: str = "stdout", limit: Optional[int] = None) -> str:
        data = self.stdout if stream == "stdout" else self.stderr
        s = data.decode("utf-8", errors="replace")
        return s if limit is None or len(s) <= limit else s[:limit]


class ProcessTracker:
    """Registry of live child processes (for shutdown / diagnostics)."""

    def __init__(self) -> None:
        self._procs: Dict[int, asyncio.subprocess.Process] = {}

    def add(self, proc: "asyncio.subprocess.Process") -> None:
        self._procs[proc.pid] = proc
        metrics.gauge_add("astra_processes_running", 1)

    def remove(self, proc: "asyncio.subprocess.Process") -> None:
        if self._procs.pop(proc.pid, None) is not None:
            metrics.gauge_add("astra_processes_running", -1)

    def __len__(self) -> int:
        return len(self._procs)

    def kill_all(self) -> int:
        n = 0
        for proc in list(self._procs.values()):
            if proc.returncode is None:
                _signal_group(proc, signal.SIGKILL)
                n += 1
        return n


_default_tracker = ProcessTracker()


def default_tracker() -> ProcessTracker:
    return _default_tracker


def build_env(allow: Optional[Iterable[str]] = None, extra: Optional[Mapping[str, str]] = None, *, inherit: bool = False) -> Dict[str, str]:
    """Build a minimal environment.  ``allow`` names are copied from
    ``os.environ`` if present; ``extra`` is applied last."""
    if inherit:
        env = dict(os.environ)
    else:
        env = {}
        for name in (DEFAULT_ENV_ALLOW if allow is None else allow):
            if name in os.environ:
                env[name] = os.environ[name]
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def safe_resolve(path: str, root: str) -> Optional[str]:
    """Resolve ``path`` (following symlinks) and return it only if it stays
    inside ``root``; otherwise None.  Symlink-safe at check time (see the
    sandbox for TOCTOU-proof isolation)."""
    try:
        base = os.path.realpath(root)
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path))
        if os.path.commonpath([base, full]) != base:
            return None
        return full
    except (ValueError, OSError):
        return None


_warned_prlimit = False


def _wrap_prlimit(argv: List[str], limits: Limits) -> List[str]:
    global _warned_prlimit
    exe = shutil.which("prlimit")
    if not exe:
        if not _warned_prlimit:
            logger.warning("prlimit not found — resource limits are NOT enforced (install util-linux or use a sandbox).")
            _warned_prlimit = True
        return argv
    flags: List[str] = []
    if limits.cpu_seconds:
        flags.append(f"--cpu={int(limits.cpu_seconds)}")
    if limits.memory_bytes:
        flags.append(f"--as={int(limits.memory_bytes)}")
    if limits.file_size_bytes:
        flags.append(f"--fsize={int(limits.file_size_bytes)}")
    if limits.max_processes:
        flags.append(f"--nproc={int(limits.max_processes)}")
    if not flags:
        return argv
    return [exe, *flags, "--", *argv]


def _signal_group(proc: "asyncio.subprocess.Process", sig: int) -> None:
    try:
        if _IS_POSIX:
            os.killpg(proc.pid, sig)
        else:  # pragma: no cover
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_soon(proc: "asyncio.subprocess.Process", grace: float) -> None:
    """Non-awaiting cleanup used when our own task is being cancelled:
    SIGTERM now, SIGKILL after ``grace``, and reap in the background."""
    _signal_group(proc, signal.SIGTERM)
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(grace, _signal_group, proc, signal.SIGKILL)
        asyncio.ensure_future(_reap(proc))
    except RuntimeError:  # pragma: no cover - no loop
        _signal_group(proc, signal.SIGKILL)


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    try:
        await proc.wait()
    except Exception:
        pass


async def _terminate(proc: "asyncio.subprocess.Process", grace: float) -> None:
    if proc.returncode is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            await proc.wait()
    # Always sweep the group so orphaned grandchildren don't outlive the call.
    _signal_group(proc, signal.SIGKILL)


class _Overflow(Exception):
    pass


async def run_process(
    spec: ProcessSpec,
    *,
    on_output: Optional[Callable[[str, bytes], Awaitable[None]]] = None,
    tracker: Optional[ProcessTracker] = None,
) -> ProcResult:
    """Run ``spec`` and return a ProcResult.  Never uses a shell.

    Raises only ``asyncio.CancelledError`` (after killing the process group).
    Spawn failures, timeouts and non-zero exits are reported in the result.
    """
    tracker = tracker or _default_tracker
    argv = list(spec.argv)
    if not argv:
        return ProcResult(spawn_error="empty command")
    if spec.limits:
        argv = _wrap_prlimit(argv, spec.limits)

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=spec.env,
            start_new_session=_IS_POSIX,
        )
    except FileNotFoundError:
        return ProcResult(spawn_error=f"command not found: {argv[0]}", duration=time.monotonic() - t0)
    except (PermissionError, OSError) as exc:
        return ProcResult(spawn_error=f"could not start '{argv[0]}': {exc}", duration=time.monotonic() - t0)

    tracker.add(proc)
    cap = spec.max_output_bytes
    result = ProcResult()
    bufs = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}

    async def pump(stream: Optional[asyncio.StreamReader], name: str) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(65536)
            if not data:
                return
            totals[name] += len(data)
            room = cap - len(bufs[name])
            if room > 0:
                bufs[name].extend(data[:room])
            if totals[name] > cap:
                setattr(result, f"{name}_truncated", True)
            if on_output is not None:
                try:
                    await on_output(name, data)
                except Exception:
                    logger.exception("on_output hook raised")
            if totals[name] > cap * max(1, spec.overflow_kill_factor):
                raise _Overflow()

    async def feed() -> None:
        if spec.stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(spec.stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def communicate() -> None:
        await asyncio.gather(feed(), pump(proc.stdout, "stdout"), pump(proc.stderr, "stderr"), proc.wait())

    try:
        await asyncio.wait_for(communicate(), spec.timeout)
    except asyncio.TimeoutError:
        result.timed_out = True
        result.killed = True
        await _terminate(proc, spec.grace)
        metrics.inc("astra_process_killed_total", reason="timeout", tool=spec.label or "?")
    except _Overflow:
        result.killed = True
        result.stdout_truncated = result.stdout_truncated or totals["stdout"] > cap
        result.stderr_truncated = result.stderr_truncated or totals["stderr"] > cap
        await _terminate(proc, spec.grace)
        metrics.inc("astra_process_killed_total", reason="overflow", tool=spec.label or "?")
    except asyncio.CancelledError:
        _kill_soon(proc, min(spec.grace, 1.0))
        tracker.remove(proc)
        metrics.inc("astra_process_killed_total", reason="cancelled", tool=spec.label or "?")
        raise
    else:
        # Normal exit: still sweep the process group for stragglers.
        _signal_group(proc, signal.SIGKILL)
    finally:
        if proc.returncode is not None:
            tracker.remove(proc)

    tracker.remove(proc)
    result.returncode = proc.returncode
    result.stdout = bytes(bufs["stdout"])
    result.stderr = bytes(bufs["stderr"])
    result.duration = time.monotonic() - t0
    return result
