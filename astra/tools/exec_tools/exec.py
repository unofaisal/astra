# astra/tools/exec_tools/exec.py
"""An OPT-IN generic command runner.

Industry harnesses (Claude Code, Codex CLI, Goose) give the model a shell
tool plus instructions ("skills") instead of wrapping every CLI.  astra
supports that pattern — but it is **disabled unless you turn it on**, and
even then it is constrained:

  * no shell (argv is exec'd directly — no pipes/redirects/expansion);
  * an explicit allow-list of program names (``allowed_commands``);
  * runs only inside the workspace (cwd confined; symlink-safe);
  * minimal environment, byte-capped output, wall-clock timeout, killed
    process group, sandbox runner when available (``require_sandbox`` to
    fail closed).

Enable it (typically at startup):

    from astra.tools.exec_tools.exec import configure_exec
    configure_exec(enabled=True, allowed_commands=["git", "ls", "cat", "rg"],
                   workspace="/srv/agent-workspaces/acme")
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

from astra.tools.core import DENIED, INVALID_ARGS, TIMEOUT, TOOL_ERROR, UNAVAILABLE, ToolContext, ToolError
from astra.tools.decorator import tool
from astra.tools.process import ProcessSpec, build_env, safe_resolve


@dataclass
class ExecConfig:
    enabled: bool = False
    allowed_commands: List[str] = field(default_factory=list)
    workspace: Optional[str] = None
    max_timeout: float = 60.0
    default_timeout: float = 30.0
    max_output_bytes: int = 200_000
    env_allow: Optional[List[str]] = None
    network: bool = False
    require_sandbox: bool = False


_config = ExecConfig()


def configure_exec(**kwargs) -> ExecConfig:
    """Enable/configure exec_command.  Pass ExecConfig fields as keywords."""
    global _config
    _config = ExecConfig(**kwargs)
    return _config


def _enabled() -> bool:
    return _config.enabled and bool(_config.allowed_commands)


@tool(schema_name="exec_command", check=_enabled, destructive=True, open_world=True, timeout=0)
async def exec_command(args: dict, ctx: ToolContext) -> dict:
    from astra.tools.runtime import get_default_runtime
    from astra.tools.sandbox import SandboxPolicy

    cfg = _config
    runtime = ctx.runtime or get_default_runtime()
    workspace = cfg.workspace or ctx.workspace or runtime.workspace
    if not workspace:
        raise ToolError("exec_command has no workspace configured.", error_type=UNAVAILABLE)

    try:
        argv = shlex.split(args["command"])
    except ValueError as exc:
        raise ToolError(f"Could not parse command: {exc}", error_type=INVALID_ARGS)
    if not argv:
        raise ToolError("Empty command.", error_type=INVALID_ARGS)
    program = os.path.basename(argv[0])
    if argv[0] != program and program not in cfg.allowed_commands:
        raise ToolError("Give the program name only (no path).", error_type=INVALID_ARGS)
    if program not in cfg.allowed_commands:
        raise ToolError(
            f"'{program}' is not an allowed command.",
            error_type=DENIED,
            hint="Allowed: " + ", ".join(sorted(cfg.allowed_commands)),
        )
    argv[0] = program

    cwd = workspace
    if args.get("cwd"):
        cwd = safe_resolve(args["cwd"], workspace)
        if cwd is None:
            raise ToolError("cwd is outside the workspace.", error_type=DENIED)
    if not os.path.isdir(cwd):
        raise ToolError(f"Working directory does not exist: {cwd}", error_type=UNAVAILABLE)

    timeout = min(float(args.get("timeout_seconds") or cfg.default_timeout), cfg.max_timeout)
    remaining = ctx.remaining()
    if remaining is not None:
        timeout = min(timeout, max(0.1, remaining))
    spec = ProcessSpec(
        argv=argv,
        cwd=cwd,
        env=build_env(cfg.env_allow),
        timeout=timeout,
        max_output_bytes=cfg.max_output_bytes,
        label="exec_command",
        sandbox_policy=SandboxPolicy(network=cfg.network, writable=[os.path.realpath(workspace)]),
    )
    runner = runtime.runner_for("required" if cfg.require_sandbox else "auto")
    if runner is None:
        raise ToolError("exec_command requires a sandbox but none is available.", error_type=DENIED)

    from astra.concurrency import LimiterBusy

    try:
        async with runtime.process_limiter.slot(runtime.queue_timeout):
            res = await runner.run(spec, on_output=ctx.emit, tracker=runtime.tracker)
    except LimiterBusy as exc:
        raise ToolError(str(exc), error_type="busy", retryable=True)
    if res.spawn_error:
        raise ToolError(res.spawn_error, error_type=UNAVAILABLE)
    if res.timed_out:
        raise ToolError(f"Command timed out after {timeout:g}s", error_type=TIMEOUT, retryable=False)
    return {
        "exit_code": res.returncode,
        "stdout": res.text("stdout"),
        "stderr": res.text("stderr"),
        "truncated": bool(res.stdout_truncated or res.stderr_truncated),
    }
