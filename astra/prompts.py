"""System prompt assembly.

The original harness baked a hardcoded ERPNext identity/style prompt
plus tool-guidance/chart-instructions into setup.py, and fetched a
"volatile" per-session context block (user, company, fiscal year, ...)
fresh on every call. Astra keeps the *shape* — a prompt is built from
zero or more independent pieces joined together — but makes every piece
a plain callable the user supplies. Prompts are just text; how you fetch
that text (a markdown file, a DB row, a hardcoded string, a template
rendered per-session) is entirely up to you.

Usage:

    from astra.prompts import PromptBuilder, from_markdown_file

    builder = PromptBuilder(
        parts=[
            from_markdown_file("prompts/identity.md"),   # static
            from_markdown_file("prompts/tool_guidance.md"),
            lambda: my_session_context_block(),           # dynamic, per-call
        ]
    )
    system_prompt = builder.build()

Nothing here caches by default — a static markdown-file loader reads the
file every call (cheap; cache it yourself with functools.lru_cache if
you want the old "cached per process" behavior). Dynamic parts (session
context, user identity) should NOT be cached, since they vary per call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

PromptPart = Callable[[], str | None]


def from_markdown_file(path: str | Path) -> PromptPart:
    """Returns a PromptPart that reads the given markdown/text file fresh
    on every call. Missing files degrade to an empty string rather than
    raising, so a not-yet-created prompt file doesn't break assembly.
    """
    p = Path(path)

    def _load() -> str | None:
        try:
            return p.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None

    return _load


def from_text(text: str) -> PromptPart:
    """Wrap a static string as a PromptPart."""
    return lambda: text


class PromptBuilder:
    """Joins prompt parts (in order) with blank lines, skipping any part
    that returns None/empty. Call build() fresh whenever you need the
    prompt — cheap unless your parts do expensive I/O, in which case
    cache the individual part, not the builder.
    """

    def __init__(self, parts: list[PromptPart] | None = None) -> None:
        self.parts: list[PromptPart] = list(parts or [])

    def add(self, part: PromptPart) -> "PromptBuilder":
        self.parts.append(part)
        return self

    def build(self) -> str:
        rendered = []
        for part in self.parts:
            try:
                text = part()
            except Exception:
                text = None
            if text:
                rendered.append(text.strip())
        return "\n\n".join(rendered)


def load_skills_index(skills_dir: str | Path, intro: str | None = None) -> PromptPart:
    """Convenience PromptPart: lists every *.md file in skills_dir as a
    one-line index (name + first line as a description), the same idea
    as the original SKILLS_INDEX_INTRO + list_skills() block. Pair with
    a `view_skill`-style tool (see astra.tools.skill_tools) that reads
    the full file content on demand, so the system prompt only carries
    an index, not every skill's full text.
    """
    d = Path(skills_dir)

    def _load() -> str | None:
        if not d.is_dir():
            return None
        lines = []
        for f in sorted(d.glob("*.md")):
            first_line = ""
            try:
                first_line = f.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
            except Exception:
                pass
            lines.append(f"- **{f.stem}**: {first_line}" if first_line else f"- **{f.stem}**")
        if not lines:
            return None
        header = intro or "Below is an index of available skills. Use `view_skill` to read a skill's full content before acting on it."
        return header + "\n\n" + "\n".join(lines)

    return _load
