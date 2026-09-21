# tests/test_loader_module_identity.py
"""Regression test for a real bug: the dynamic tool loader used to load
every tool file under a synthetic module name (astra_tools.X.Y),
disconnected from the module you get via a normal `import
astra.tools.X.Y`. Any tool relying on module-level configuration state
(configure_delegate, configure_skills_dir) would silently never see
configuration set through the normally-imported module, because the
registry's actual callable lived in a completely different module
object with its own separate globals.

The fix: the loader now imports via Python's real import system
whenever the tool file sits inside a proper installed package (has an
__init__.py chain reaching sys.path), so both import paths resolve to
the exact same cached module object.
"""
import sys

from astra.tools.decorator import ToolRegistry
from astra.tools.loader import load_tools


def test_builtin_tool_packs_load_as_real_modules_not_synthetic_ones():
    registry = ToolRegistry()
    load_tools(registry, "astra/tools")

    for tool_name, expected_prefix in [
        ("delegate_task", "astra.tools.delegate_tools"),
        ("create_skill", "astra.tools.skill_tools"),
        ("request_clarification", "astra.tools.clarify_approval_tools"),
    ]:
        fn = registry.executors[tool_name]
        assert fn.__module__.startswith(expected_prefix), (
            f"{tool_name} loaded under {fn.__module__!r} — expected a real "
            f"package module starting with {expected_prefix!r}, not a "
            f"synthetic 'astra_tools.*' one, or module-level configure_*() "
            f"calls elsewhere in the app become invisible to it."
        )


def test_module_level_config_is_visible_to_the_registry_wired_function():
    """The actual regression: configure_delegate() called via a normal
    import must be visible to delegate_task as invoked through the
    registry — this is exactly what silently broke before the fix."""
    registry = ToolRegistry()
    load_tools(registry, "astra/tools")

    import astra.tools.delegate_tools.delegate as delegate_module

    delegate_module._context = None  # reset in case another test left it configured
    wired_fn = registry.executors["delegate_task"]
    assert sys.modules[wired_fn.__module__] is delegate_module
    assert sys.modules[wired_fn.__module__]._context is None

    from astra.tools.delegate_tools.delegate import DelegateContext, configure_delegate

    sentinel = object()
    configure_delegate(DelegateContext(resolve_agent=lambda n: sentinel, store=None, config=None, registry=registry))

    assert sys.modules[wired_fn.__module__]._context is not None
    assert sys.modules[wired_fn.__module__]._context.resolve_agent("anything") is sentinel

    delegate_module._context = None  # clean up for other tests
