# astra/tools/skill_tools/skills.py
"""Skills as plain markdown files on disk — the generic replacement for
the original "Skill" doctype.

Convention (matches astra.prompts.load_skills_index, so your system
prompt's skill index and these tools stay in sync automatically):
  <skills_dir>/<name>.md, first line "# Description" used as the
  one-line summary, everything else is the skill's content.

Point this at a directory once at startup via configure_skills_dir();
defaults to "./skills" if never called.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from astra.tools.decorator import tool

_skills_dir = Path("skills")


def configure_skills_dir(path: str | Path) -> None:
    global _skills_dir
    _skills_dir = Path(path)
    _skills_dir.mkdir(parents=True, exist_ok=True)


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip()).strip("-").lower()


def _read_skill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    description = lines[0].lstrip("# ").strip() if lines else ""
    content = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    return {"name": path.stem, "description": description, "content": content}


@tool(schema_name="list_skills")
def list_skills(args: dict, **kwargs) -> str:
    limit = args.get("limit") or 20
    if not _skills_dir.is_dir():
        return json.dumps([])
    files = sorted(_skills_dir.glob("*.md"))[:limit]
    out = []
    for f in files:
        try:
            skill = _read_skill(f)
            out.append({"name": skill["name"], "description": skill["description"]})
        except Exception:
            continue
    return json.dumps(out, default=str)


@tool(schema_name="view_skill")
def view_skill(args: dict, **kwargs) -> str:
    skill_name = args.get("skill_name")
    if not skill_name:
        return json.dumps({"error": "skill_name is required"})
    path = _skills_dir / f"{_slugify(skill_name)}.md"
    if not path.exists():
        return json.dumps({"error": f"Skill '{skill_name}' does not exist"})
    try:
        return json.dumps(_read_skill(path), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(schema_name="create_skill")
def create_skill(args: dict, **kwargs) -> str:
    name = args.get("name")
    description = args.get("description")
    content = args.get("content") or ""
    if not name:
        return json.dumps({"error": "name is required"})
    if not description:
        return json.dumps({"error": "description is required"})

    slug = _slugify(name)
    if not slug:
        return json.dumps({"error": f"'{name}' does not produce a valid filename"})

    _skills_dir.mkdir(parents=True, exist_ok=True)
    path = _skills_dir / f"{slug}.md"
    if path.exists():
        return json.dumps({"error": f"Skill '{slug}' already exists.", "error_type": "already_exists", "name": slug})

    path.write_text(f"# {description}\n\n{content}\n", encoding="utf-8")
    return json.dumps({"name": slug, "description": description, "status": "created"})
