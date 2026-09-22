EXEC_COMMAND = {
    "name": "exec_command",
    "description": (
        "Run a command-line program and return its exit code, stdout and stderr. "
        "The command is executed directly (NO shell): pipes, redirects, globbing, '&&' and "
        "environment expansion do not work — pass a single program with its arguments. "
        "Only an allow-listed set of programs can run, inside the configured workspace."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The program and its arguments, e.g. `git status --short`. Quote arguments containing spaces.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory, relative to the workspace.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "description": "Optional time limit (capped by the host's maximum).",
            },
        },
        "required": ["command"],
    },
}
