"""Minimal SKILL.md frontmatter reader (stdlib only).

Handles what skill files actually use: `key: value`, quoted values, and `|` / `>` block
scalars. It is deliberately lenient (like Claude Code's own loader, which accepts
descriptions that strict YAML rejects), and it separately reports strict-YAML hazards so the
linter can warn about them.
"""
from __future__ import annotations

import re
from pathlib import Path

KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(?:\s+(.*))?$")


class FrontmatterError(ValueError):
    pass


def split(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Raises FrontmatterError if there is none."""
    if not text.startswith("---"):
        raise FrontmatterError("file does not start with '---'")
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise FrontmatterError("no closing '---' for the frontmatter")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.replace('\\"', '"') if v[0] == '"' else inner.replace("''", "'")
    return v


def parse(block: str) -> tuple[dict[str, str], list[str]]:
    """Parse a frontmatter block. Returns (fields, hazards)."""
    fields: dict[str, str] = {}
    hazards: list[str] = []
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = KEY_RE.match(line)
        if not m:
            if line.startswith((" ", "\t")):  # nested value of a previous key we do not model
                i += 1
                continue
            raise FrontmatterError(f"line {i + 1} is not 'key: value': {line[:60]!r}")
        key, raw = m.group(1), (m.group(2) or "")
        if raw.strip() in ("|", ">", "|-", ">-", "|+", ">+"):
            style = raw.strip()[0]
            chunk = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                chunk.append(lines[i].strip())
                i += 1
            fields[key] = ("\n" if style == "|" else " ").join(chunk).strip()
            continue
        value = _unquote(raw)
        if raw and raw.strip()[0] not in "\"'" and ": " in raw:
            hazards.append(f"'{key}' is an unquoted value containing ': ' "
                           "(strict YAML says 'mapping values are not allowed')")
        fields[key] = value
        i += 1
    return fields, hazards


def read(path: str | Path) -> tuple[dict[str, str], list[str], str]:
    """Read a SKILL.md. Returns (fields, hazards, body)."""
    text = Path(path).read_text(encoding="utf-8")
    block, body = split(text)
    fields, hazards = parse(block)
    return fields, hazards, body
