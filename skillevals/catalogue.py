"""The skill catalogue: every local skill's name + description, read-only.

A trigger eval is competitive: a skill is picked (or not) *against all the other skills*.
So the judge always sees the whole catalogue, not one description on its own.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter

DEFAULT_SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    hazards: list[str] = field(default_factory=list)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load(skills_dir: Path = DEFAULT_SKILLS_DIR) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not Path(skills_dir).is_dir():
        raise SystemExit(f"skills dir not found: {skills_dir} (pass --skills-dir, before the subcommand)")
    for d in sorted(Path(skills_dir).iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            fields, hazards, _ = frontmatter.read(f)
        except (frontmatter.FrontmatterError, UnicodeDecodeError):
            continue  # the linter reports these; the catalogue just skips them
        name = fields.get("name") or d.name
        out[name] = Skill(name, " ".join(fields.get("description", "").split()), f, hazards)
    return out


def resolve_draft(d: str) -> Path:
    """A set's `draft` path: `~` is expanded, and a relative path is read from the repo root
    (not the cwd), so `examples/skills/<name>/SKILL.md` works from anywhere."""
    p = Path(os.path.expanduser(d))
    return p if p.is_absolute() else REPO_ROOT / p


def with_overrides(cat: dict[str, Skill], overrides: dict[str, str]) -> dict[str, Skill]:
    """Return a COPY of the catalogue with some descriptions replaced in memory.

    This is how a 'broken description' is tested without ever touching ~/.claude/skills.
    """
    new = dict(cat)
    for name, desc in overrides.items():
        if name not in new:
            raise KeyError(f"override for unknown skill {name!r}")
        s = new[name]
        new[name] = Skill(s.name, desc, s.path, s.hazards)
    return new


def with_drafts(cat: dict[str, Skill], sets: list[dict]) -> dict[str, Skill]:
    """Return a COPY of the catalogue plus any set's uninstalled `draft` SKILL.md (in memory).

    Lets a skill be trigger-tested before it is copied into ~/.claude/skills. An installed
    skill of the same name wins: the draft never shadows the real thing."""
    new = dict(cat)
    for s in sets:
        d = s.get("draft")
        if not d or s["skill"] in new:
            continue
        f = resolve_draft(d)
        if not f.is_file():
            raise SystemExit(f"draft not found: {f} (set {s['skill']!r})")
        fields, hazards, _ = frontmatter.read(f)
        name = fields.get("name") or s["skill"]
        new[name] = Skill(name, " ".join(fields.get("description", "").split()), f, hazards)
    return new


def render(cat: dict[str, Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in cat.values())


def catalogue_sha(cat: dict[str, Skill]) -> str:
    return sha(render(cat))
