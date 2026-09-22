# Declarative CLI tools (manifests)

This directory is an EXAMPLE, not auto-loaded by `load_tools("astra/tools")`
(it has no `schema.py`, so on its own `load_tools` would only pick up its
`tool.json`, which it does — try it):

    from astra.tools.decorator import ToolRegistry
    from astra.tools.loader import load_tools
    registry = ToolRegistry()
    load_tools(registry, "astra/tools/examples_manifest")
    # registry now has "git_status", no Python code involved

See astra/tools/manifest.py's module docstring for the full manifest
schema (exec.command as argv — never a shell string — placeholders like
`{path}` / `{path?}` / `{path*}`, path confinement via `x-astra-path`,
sandboxing, env allowlisting, output shaping).
