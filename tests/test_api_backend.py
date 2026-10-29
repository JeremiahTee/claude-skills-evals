"""Offline tests for the workspace flag, the price table and spend cap, and the anthropic:
backend (docs/design-api-backend.md). No network and no SDK: the API is a fake client."""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillevals import __main__ as cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = str(ROOT / "examples" / "skills")


def tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and ".git" not in rel.parts and "__pycache__" not in rel.parts:
            out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def quiet(argv: list[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return cli.main(argv)


class Workspace(unittest.TestCase):
    def test_workspace_run_leaves_the_repo_unchanged(self):
        before = tree_digest(ROOT)
        ws = Path(tempfile.mkdtemp())
        quiet(["--skills-dir", EXAMPLES, "--workspace", str(ws), "trigger", "--judge", "scripted", "--no-lint"])
        quiet(["--skills-dir", EXAMPLES, "--workspace", str(ws), "canary",
               "--agent", "mutant:always-refuse", "--grader", "scripted"])
        self.assertEqual(tree_digest(ROOT), before)
        self.assertTrue(list(ws.glob("results/*/*.jsonl")), "trigger results go to the workspace")
        self.assertTrue(list(ws.glob("results/canary/*/*.jsonl")), "canary results go to the workspace")
        self.assertTrue((ws / "HISTORY.md").is_file() and (ws / "REPORT.md").is_file())


if __name__ == "__main__":
    unittest.main()
