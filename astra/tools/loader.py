# astra/tools/loader.py
"""Dynamically loads tool groups from a directory.

Convention (unchanged from the original harness): tools/<group>/schema.py
holds dict-shaped OpenAI function schemas; tools/<group>/*.py holds
@tool-decorated implementation functions wired to those schemas by name.

This module has no framework dependency — it never touches a database
or a "sync" doctype. If you want an on/off switch for individual tools
(the old Tool Group / Agent Tool doctypes), build it yourself on top of
ToolRegistry.disable() / get_tool_names() once tools are loaded.

MODULE IDENTITY MATTERS for tools with configuration state (e.g.
skill_tools.configure_skills_dir(), delegate_tools.configure_delegate()):
if a tool file is imported once via `import astra.tools.foo.bar` (by
your own code, to call configure_*()) and loaded AGAIN here under a
synthetic module name, Python treats them as two unrelated module
objects with two separate copies of any module-level state — calling
configure_*() on one is invisible to the other. So this loader always
tries Python's real import machinery first (which shares state
correctly via sys.modules) and only falls back to a synthetic
file-based load for directories that aren't a real importable package
(e.g. an arbitrary external tools/ directory with no __init__.py chain).
"""
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from .decorator import ToolRegistry

logger = logging.getLogger(__name__)


def load_tools(registry: ToolRegistry, tools_dir: str | Path) -> None:
    base = Path(tools_dir)

    for tool_group_dir in base.iterdir():
        if not tool_group_dir.is_dir() or tool_group_dir.name.startswith("_"):
            continue

        # 1. Load schemas first
        schema_file = tool_group_dir / "schema.py"
        if schema_file.exists():
            module = _load_module(schema_file, "schema")
            if module is not None:
                _register_schemas(module, schema_file, registry)
        else:
            logger.warning(f"Skipping {tool_group_dir.name}: missing schema.py")

        # 2. Load and wire implementations
        for py_file in tool_group_dir.glob("*.py"):
            if py_file.name.startswith("_") or py_file.name == "schema.py":
                continue
            module = _load_module(py_file, py_file.stem)
            if module is not None:
                _register_implementations(module, py_file, registry)


def _dotted_name_if_real_package(py_file: Path) -> str | None:
    """If py_file sits inside a real Python package (an unbroken chain
    of __init__.py up to a directory that's actually on sys.path),
    return its proper dotted module name so it can be loaded with
    importlib.import_module() — sharing module state with however else
    it might already be imported. Returns None if it's not part of a
    real package (e.g. an arbitrary external tools/ directory), so the
    caller falls back to a synthetic per-file load instead.
    """
    resolved_sys_path = set()
    for entry in sys.path:
        try:
            # sys.path entries are often relative ('.', '') or symlinked —
            # resolve them the same way py_file itself is resolved below,
            # or a same-directory comparison silently never matches.
            resolved_sys_path.add(Path(entry or ".").resolve())
        except OSError:
            continue

    resolved = py_file.resolve()
    parts = [resolved.stem]
    current = resolved.parent
    while (current / "__init__.py").exists():
        parts.insert(0, current.name)
        parent = current.parent
        if parent in resolved_sys_path or current in resolved_sys_path:
            return ".".join(parts)
        if parent == current:  # reached filesystem root without finding sys.path
            break
        current = parent
    return None


def _load_module(py_file: Path, label: str) -> ModuleType | None:
    dotted = _dotted_name_if_real_package(py_file)
    if dotted:
        try:
            module = importlib.import_module(dotted)
            resolved_module_file = getattr(module, "__file__", None)
            if resolved_module_file and Path(resolved_module_file).resolve() == py_file.resolve():
                return module
            # Name collided with something unrelated already in
            # sys.modules — fall through to the synthetic load below
            # rather than risk using the wrong module.
        except Exception as exc:
            logger.error(f"Failed to import {py_file} as {dotted}: {exc}")
            return None

    # Fallback: synthetic per-file module, for tool directories that
    # aren't part of a real installed package. No module-level
    # configure_*() state will be shareable with outside code for these
    # — fine for stateless tools, a real package is needed for stateful
    # ones (see the module docstring above).
    module_name = f"astra_tools.{py_file.parent.name}.{label}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        logger.error(f"Failed to load {py_file}: {exc}")
        return None


def _register_schemas(module: ModuleType, schema_file: Path, registry: ToolRegistry) -> list[str]:
    """Finds all dict-shaped OpenAI function schemas in an already-loaded
    schema module and registers them. Returns the list of tool names."""
    schemas = {
        val["name"]: val
        for val in vars(module).values()
        if isinstance(val, dict) and "name" in val and "parameters" in val
    }
    if schemas:
        registry.load_schemas(schemas)
        logger.info(f"Loaded {len(schemas)} schemas from {schema_file.parent.name}/schema.py")
    return list(schemas.keys())


def _register_implementations(module: ModuleType, py_file: Path, registry: ToolRegistry) -> None:
    """Finds @tool decorated functions in an already-loaded module and
    wires them to their pre-registered schemas."""
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if callable(obj) and getattr(obj, "__is_tool__", False):
            try:
                registry.register_tool(obj)
                logger.debug(f"Wired executor '{obj.__schema_name__}' from {py_file.name}")
            except Exception as exc:
                logger.error(f"Failed to wire {attr_name}: {exc}")
