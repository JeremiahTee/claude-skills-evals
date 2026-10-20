"""Deterministic checks. No model involved: same input, same answer, every time.

Levels: FAIL breaks the skill (or its promise), WARN is a smell worth a look, PASS is fine,
SKIP means the check was not run (and says why). Only FAIL makes `ok` false.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PATH_RE = re.compile(r"(?:~|/Users/[A-Za-z0-9_.-]+|\$HOME)/[^\s`'\"()\]|,;]+")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
PLACEHOLDER = re.compile(r"[<>{}*$]|YYYY|MM-DD|\.\.\.|…|NN\b|XX")
TEXT_EXT = {".md", ".sh", ".py"}
MAX_DESC = 1024   # Agent Skills limit on description length, as documented


@dataclass
class Check:
    skill: str
    check: str
    level: str      # PASS | WARN | FAIL | SKIP
    detail: str = ""


def _files(skill_dir: Path) -> list[Path]:
    """SKILL.md plus top-level and scripts/ text files. Never walks deep trees (venvs)."""
    out = []
    for p in [skill_dir, skill_dir / "scripts"]:
        if p.is_dir():
            out += [f for f in sorted(p.iterdir()) if f.is_file() and f.suffix in TEXT_EXT]
    return out


def cited_paths(skill_dir: Path) -> list[str]:
    found: list[str] = []
    for f in _files(skill_dir):
        text = f.read_text(encoding="utf-8", errors="replace")
        # A path inside `backticks` may contain spaces ("~/My Folder/"): take the whole span.
        spans = [m.group(1) for m in BACKTICK_RE.finditer(text) if PATH_RE.match(m.group(1))]
        rest = BACKTICK_RE.sub(lambda m: "" if PATH_RE.match(m.group(1)) else m.group(0), text)
        for p in spans + [m.group(0) for m in PATH_RE.finditer(rest)]:
            p = p.strip().rstrip(".:;,)`'\"")
            if PLACEHOLDER.search(p.split("/", 1)[1] if "/" in p else p):
                continue
            if p not in found:
                found.append(p)
    return found


def _expand(p: str) -> str:
    return os.path.expanduser(p.replace("$HOME", "~", 1))


def lint_skill(skill_dir: Path, path_ignore: list[str] | None = None) -> list[Check]:
    name = skill_dir.name
    out: list[Check] = []
    f = skill_dir / "SKILL.md"
    if not f.is_file():
        return [Check(name, "skill-md-exists", "FAIL", f"{f} missing")]
    try:
        fields, hazards, body = frontmatter.read(f)
    except frontmatter.FrontmatterError as e:
        return [Check(name, "frontmatter-parses", "FAIL", str(e))]
    out.append(Check(name, "frontmatter-parses", "PASS"))

    n = fields.get("name", "")
    if not n:
        out.append(Check(name, "name-present", "FAIL", "no name field"))
    elif n != name:
        out.append(Check(name, "name-matches-dir", "FAIL", f"name {n!r} != folder {name!r}"))
    elif not NAME_RE.match(n):
        out.append(Check(name, "name-format", "FAIL", "lowercase letters, digits, hyphens, <=64"))
    else:
        out.append(Check(name, "name-matches-dir", "PASS"))

    d = " ".join(fields.get("description", "").split())
    if not d:
        out.append(Check(name, "description-present", "FAIL", "empty description: the router can never pick it"))
    else:
        lvl = "FAIL" if len(d) > MAX_DESC else "PASS"
        out.append(Check(name, "description-length", lvl, f"{len(d)} chars (limit {MAX_DESC})"))
        out.append(Check(name, "description-says-when", "PASS" if d.lower().startswith("use when") else "WARN",
                         "" if d.lower().startswith("use when") else "does not start with 'Use when' (house style)"))
        has_neg = re.search(r"do not use|don't use|not for\b", d, re.I)
        out.append(Check(name, "description-has-negatives", "PASS" if has_neg else "WARN",
                         "" if has_neg else "no 'Do NOT use' clause: near-misses have nothing to steer them"))
    for h in hazards:
        out.append(Check(name, "strict-yaml-hazard", "WARN", h))
    if len(body.split()) < 20:
        out.append(Check(name, "body-nonempty", "WARN", f"body is {len(body.split())} words"))

    ignore = [re.compile(x) for x in (path_ignore or [])]
    paths = [p for p in cited_paths(skill_dir) if not any(r.search(p) for r in ignore)]
    missing = [p for p in paths if not os.path.exists(_expand(p))]
    if missing:
        out.append(Check(name, "cited-paths-exist", "FAIL",
                         f"{len(paths) - len(missing)}/{len(paths)} exist; missing: " + ", ".join(missing)))
    else:
        out.append(Check(name, "cited-paths-exist", "PASS", f"{len(paths)}/{len(paths)} exist"))
    return out


def run_selftests(skill: str, tests: list[dict], allow_network: bool, enabled: bool = True) -> list[Check]:
    """Run a set's declared self-test commands (`bash -c`, the user's rights). The CLI passes
    enabled=False unless --run-selftests is given, so a set's JSON never executes by default."""
    out: list[Check] = []
    if tests and not enabled:
        return [Check(skill, "selftests", "SKIP", f"{len(tests)} declared, not run; pass --run-selftests to run")]
    for t in tests:
        label = f"selftest:{t['name']}"
        if t.get("network") and not allow_network:
            out.append(Check(skill, label, "SKIP", "needs network; pass --network to run"))
            continue
        cwd = _expand(t.get("cwd", "~"))
        t0 = time.time()
        try:
            p = subprocess.run(["bash", "-c", t["cmd"]], cwd=cwd, capture_output=True, text=True,
                               timeout=t.get("timeout", 120))
        except subprocess.TimeoutExpired:
            out.append(Check(skill, label, "FAIL", "timeout"))
            continue
        except FileNotFoundError as e:
            out.append(Check(skill, label, "FAIL", str(e)))
            continue
        tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
        out.append(Check(skill, label, "PASS" if p.returncode == 0 else "FAIL",
                         f"exit {p.returncode} in {time.time() - t0:.1f}s; last line: {tail[0][:160]}"))
    return out
