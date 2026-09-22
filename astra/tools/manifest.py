"""Declarative CLI tools: a ``tool.json`` (or ``*.tool.json``; ``.yaml``
if PyYAML is installed) next to — or instead of — Python code.

    {
      "name": "run_eslint",
      "description": "Lint a JavaScript file and return findings as JSON.",
      "parameters": {
        "type": "object",
        "properties": { "path": {"type": "string", "x-astra-path": true} },
        "required": ["path"]
      },
      "exec": {
        "command": ["npx", "eslint", "--format", "json", "--", "{path}"],
        "timeout": 60,
        "output": "json",
        "ok_exit_codes": [0, 1],
        "env": ["PATH", "HOME"]
      },
      "annotations": {"read_only": true, "idempotent": true},
      "requires": ["npx"]
    }

Nothing here executes code at load time; the manifest is validated (typos
in placeholders, bad shapes) and bad manifests are skipped with an error
recorded in ``registry.load_errors`` instead of crashing the loader.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import Tool, ToolAnnotations
from .handlers import RESERVED, ExecSpec, ProcessHandler, _PLACEHOLDER

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")
_EXEC_KEYS = {
    "command", "cwd", "timeout", "stdin", "output", "env", "env_set", "secrets", "ok_exit_codes",
    "max_output_bytes", "reject_leading_dash", "sandbox", "network", "writable", "limits",
}
_TOP_KEYS = {
    "name", "description", "parameters", "exec", "annotations", "requires", "toolset", "timeout",
    "max_output_chars", "max_concurrency", "deferred", "breaker", "$schema", "version",
}


class ManifestError(ValueError):
    pass


def _placeholders(command: List[Any]) -> List[str]:
    names: List[str] = []
    for el in command:
        if isinstance(el, str):
            names += [m.group(1) for m in _PLACEHOLDER.finditer(el)]
        elif isinstance(el, dict):
            if "when" in el:
                names.append(str(el["when"]))
            names += _placeholders(el.get("args") or [])
    return names


def parse_manifest(data: Dict[str, Any], *, source: str = "", default_toolset: str = "default") -> Tool:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ManifestError(f"unknown manifest keys: {sorted(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ManifestError("'name' must match ^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ManifestError("'description' is required")
    params = data.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(params, dict) or params.get("type") != "object":
        raise ManifestError("'parameters' must be a JSON Schema object (type: object)")
    props = params.get("properties") or {}
    clash = [p for p in props if p in RESERVED]
    if clash:
        raise ManifestError(f"parameter names {clash} are reserved")

    ex = data.get("exec")
    if not isinstance(ex, dict):
        raise ManifestError("'exec' section is required")
    bad = set(ex) - _EXEC_KEYS
    if bad:
        raise ManifestError(f"unknown exec keys: {sorted(bad)}")
    cmd = ex.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, (str, dict)) for c in cmd):
        raise ManifestError("exec.command must be a non-empty list (argv, not a shell string)")
    if not isinstance(cmd[0], str) or _PLACEHOLDER.search(cmd[0]):
        raise ManifestError("the executable (first element of exec.command) must be a literal string")
    known = set(props) | set(RESERVED)
    for ph in _placeholders(cmd) + _placeholders([ex.get("cwd") or ""]) + _placeholders([ex.get("stdin") or ""]):
        if ph not in known:
            raise ManifestError(f"exec references '{{{ph}}}' which is not a declared parameter")

    if ex.get("output", "text") not in ("text", "json", "lines"):
        raise ManifestError("exec.output must be text, json or lines")
    if ex.get("sandbox", "auto") not in ("auto", "none", "required"):
        raise ManifestError("exec.sandbox must be auto, none or required")

    path_params = [n for n, sp in props.items() if isinstance(sp, dict) and (sp.get("x-astra-path") or sp.get("format") == "path")]
    for n, sp in props.items():  # array-of-path params
        it = sp.get("items") if isinstance(sp, dict) else None
        if isinstance(it, dict) and (it.get("x-astra-path") or it.get("format") == "path") and n not in path_params:
            path_params.append(n)

    spec = ExecSpec(
        command=cmd,
        cwd=ex.get("cwd"),
        timeout=float(ex.get("timeout", data.get("timeout", 60))),
        stdin=ex.get("stdin"),
        output=ex.get("output", "text"),
        env_allow=ex.get("env"),
        env_set={str(k): str(v) for k, v in (ex.get("env_set") or {}).items()},
        secrets=list(ex.get("secrets") or []),
        ok_exit_codes=list(ex.get("ok_exit_codes") or [0]),
        max_output_bytes=int(ex.get("max_output_bytes", 1_000_000)),
        reject_leading_dash=bool(ex.get("reject_leading_dash", True)),
        sandbox=ex.get("sandbox", "auto"),
        network=bool(ex.get("network", False)),
        writable=list(ex.get("writable") or []),
        limits=ex.get("limits"),
        path_params=path_params,
    )
    ann = data.get("annotations") or {}
    requires = list(data.get("requires") or [])
    check = (lambda reqs=requires: all(shutil.which(r) for r in reqs)) if requires else None
    return Tool(
        name=name,
        description=desc.strip(),
        parameters=params,
        handler=ProcessHandler(spec, tool_name=name),
        toolset=data.get("toolset") or default_toolset,
        annotations=ToolAnnotations(
            read_only=bool(ann.get("read_only", False)),
            destructive=bool(ann.get("destructive", False)),
            idempotent=bool(ann.get("idempotent", False)),
            open_world=bool(ann.get("open_world", False)),
        ),
        timeout=spec.timeout + 5,  # outer guard; run_process enforces spec.timeout itself
        max_output_chars=data.get("max_output_chars"),
        max_concurrency=data.get("max_concurrency"),
        check=check,
        deferred=bool(data.get("deferred", False)),
        breaker=bool(data.get("breaker", True)),
        source="manifest",
        meta={"manifest": source},
    )


def load_manifest_file(path: Path, *, default_toolset: str = "default") -> Tool:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise ManifestError("PyYAML is not installed (pip install astra[yaml]) — use a .json manifest")
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ManifestError(f"invalid JSON: {exc}")
    return parse_manifest(data, source=str(path), default_toolset=default_toolset)


def find_manifest_files(directory: Path) -> List[Path]:
    files: List[Path] = []
    for pattern in ("tool.json", "*.tool.json", "tool.yaml", "tool.yml", "*.tool.yaml", "*.tool.yml"):
        files += list(directory.glob(pattern))
    return sorted(set(files))


def discover_manifest_tools(directory: Path, *, toolset: Optional[str] = None) -> Tuple[List[Tool], List[str]]:
    tools: List[Tool] = []
    errors: List[str] = []
    for f in find_manifest_files(directory):
        try:
            tools.append(load_manifest_file(f, default_toolset=toolset or directory.name))
        except ManifestError as exc:
            msg = f"{f}: {exc}"
            errors.append(msg)
            logger.error("Skipping manifest %s", msg)
        except OSError as exc:
            errors.append(f"{f}: {exc}")
    return tools, errors
