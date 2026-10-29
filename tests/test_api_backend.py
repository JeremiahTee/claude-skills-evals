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
from skillevals import prices  # noqa: E402

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


class Prices(unittest.TestCase):
    def test_cost_counts_every_token_kind(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
        # Opus 5.5: $4 in, $20 out, $5 5-minute cache write, $0.20 cache read (per MTok, 2026-10-28)
        self.assertAlmostEqual(prices.cost("claude-opus-5-5", usage), 4 + 20 + 5 + 0.20)

    def test_haiku_long_prompt_tier(self):
        short = {"input_tokens": 100_000, "output_tokens": 0}
        long = {"input_tokens": 100_001, "output_tokens": 0}
        self.assertAlmostEqual(prices.cost("claude-haiku-5-5", short), 0.10 * 0.1)
        self.assertAlmostEqual(prices.cost("claude-haiku-5-5", long), 0.50 * 0.100001)

    def test_unknown_model_refuses(self):
        with self.assertRaises(SystemExit):
            prices.cost("claude-made-up-9", {"input_tokens": 1})

    def test_token_estimate_is_loose(self):
        self.assertEqual(prices.estimate_tokens("abcde"), 3)  # ceil(5 / 2)

    def test_worst_case_prices_input_at_cache_write_and_counts_every_call(self):
        # 10 calls x (1000 in x $5 + 500 out x $20) / 1e6 on Opus 5.5
        self.assertAlmostEqual(prices.worst_case("claude-opus-5-5", calls=10, input_tokens=1000,
                                                 max_tokens=500), 10 * (1000 * 5 + 500 * 20) / 1e6)

    def test_preflight_refuses_over_cap_and_names_both_numbers(self):
        with self.assertRaises(SystemExit) as cm:
            prices.preflight([("claude-opus-5-5", 10, 1000, 500)], max_usd=0.1)
        self.assertIn("0.15", str(cm.exception))
        self.assertIn("0.1", str(cm.exception))
        self.assertAlmostEqual(prices.preflight([("claude-opus-5-5", 10, 1000, 500)], max_usd=0.2), 0.15)


if __name__ == "__main__":
    unittest.main()
