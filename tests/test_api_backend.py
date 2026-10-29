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
from skillevals import api, behaviour, judges, prices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def setUpModule():
    """No test in this file may reach a real model: any claude -p call or real API client fails."""
    def blocked(*a, **kw):
        raise AssertionError("a test tried to run claude -p (a real, billed call)")
    def no_real_client(self):
        if self._client is None:
            raise AssertionError("a test tried to build a real API client (billed calls)")
        return self._client

    global _saved
    _saved = (behaviour._claude, judges.subprocess.run, api.Caller.client)
    behaviour._claude = blocked
    judges.subprocess.run = blocked
    api.Caller.client = no_real_client


def tearDownModule():
    behaviour._claude, judges.subprocess.run, api.Caller.client = _saved


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


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    """Stands in for anthropic.Anthropic(): records each request, returns one canned response."""

    def __init__(self, text="", model=None, stop_reason="end_turn", category=None, raises=None,
                 usage=None):
        self.requests, self.text, self.model, self.raises = [], text, model, raises
        self.stop_reason, self.category = stop_reason, category
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.messages = Obj(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        if self.raises:
            raise self.raises
        return Obj(model=self.model or kw["model"], stop_reason=self.stop_reason,
                   stop_details=Obj(category=self.category) if self.category else None,
                   content=[Obj(type="thinking", thinking=""), Obj(type="text", text=self.text)],
                   usage=Obj(**self.usage))


NAMES = {"eli5", "handoff"}


class AnthropicBackend(unittest.TestCase):
    def judge(self, client, effort=None):
        return judges.AnthropicJudge("claude-haiku-5-5", effort=effort, client=client)

    def test_judge_picks_and_prices_in_real_dollars(self):
        v = self.judge(FakeClient('{"skill": "eli5"}'))("SYS", "explain this", NAMES)
        self.assertEqual((v.picked, v.error), ("eli5", None))
        self.assertAlmostEqual(v.cost_usd, (1000 * 0.10 + 100 * 0.50) / 1e6)

    def test_request_shape(self):
        c = FakeClient('{"skill": "none"}')
        self.judge(c, effort="low")("SYS", "hi", NAMES)
        r = c.requests[0]
        self.assertEqual(r["model"], "claude-haiku-5-5")
        self.assertNotIn("temperature", r)
        self.assertEqual(r["output_config"]["effort"], "low")
        self.assertEqual(r["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(r["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("fallbacks", r)
        self.assertEqual(r["messages"], [{"role": "user", "content": "hi"}])

    def test_effort_is_in_the_label(self):
        self.assertEqual(self.judge(FakeClient(), effort="low").label, "anthropic:claude-haiku-5-5@low")
        self.assertEqual(self.judge(FakeClient()).label, "anthropic:claude-haiku-5-5@default")

    def test_other_model_answering_is_an_error(self):
        v = self.judge(FakeClient('{"skill": "eli5"}', model="claude-opus-5"))("S", "p", NAMES)
        self.assertIsNone(v.picked)
        self.assertIn("model mismatch", v.error)

    def test_refusal_is_an_error_not_a_verdict(self):
        v = self.judge(FakeClient("", stop_reason="refusal", category="cyber"))("S", "p", NAMES)
        self.assertIsNone(v.picked)
        self.assertEqual(v.error, "refusal:cyber")

    def test_truncated_reply_is_an_error(self):
        v = self.judge(FakeClient('{"skill": "el', stop_reason="max_tokens"))("S", "p", NAMES)
        self.assertEqual((v.picked, v.error), (None, "max_tokens"))

    def test_client_exception_is_a_row_error(self):
        v = self.judge(FakeClient(raises=RuntimeError("boom")))("S", "p", NAMES)
        self.assertIsNone(v.picked)
        self.assertIn("RuntimeError", v.error)
        self.assertEqual(v.cost_usd, 0.0)

    def test_unpriced_model_refuses_at_construction(self):
        with self.assertRaises(SystemExit):
            judges.AnthropicJudge("claude-made-up-9", client=FakeClient())

    def test_make_does_not_need_the_sdk_until_a_call(self):
        j = judges.make("anthropic:claude-opus-5-5")
        self.assertEqual(j.label, "anthropic:claude-opus-5-5@default")

    def test_agent_is_free_text(self):
        c = FakeClient("I will not push.")
        r = behaviour.AnthropicAgent("claude-sonnet-5-5", client=c)("SKILL BODY", "context")
        self.assertEqual((r.text, r.error), ("I will not push.", None))
        self.assertNotIn("format", c.requests[0].get("output_config", {}))

    def test_grader_uses_a_schema_and_parses(self):
        out = '{"grades": [{"n": 1, "behaviour": "refuse", "meets_pass_if": true, "why": "declines"}]}'
        c = FakeClient(out)
        case = {"id": "can-01", "vector": "v", "context": "c", "task": "t", "pass_if": "p"}
        grades, raw = behaviour.AnthropicGrader("claude-fable-5-1", client=c)(case, ["no"])
        self.assertEqual(grades[0]["behaviour"], "refuse")
        self.assertEqual(c.requests[0]["output_config"]["format"]["type"], "json_schema")
        self.assertIsNone(raw.error)


class ApiPreflight(unittest.TestCase):
    def test_trigger_refuses_before_any_call_when_worst_case_passes_cap(self):
        with self.assertRaises(SystemExit) as cm:
            quiet(["--skills-dir", EXAMPLES, "--workspace", tempfile.mkdtemp(), "trigger",
                   "--judge", "anthropic:claude-opus-5-5", "--max-usd", "0.001", "--no-lint"])
        self.assertIn("worst case", str(cm.exception))

    def test_canary_counts_agent_and_grader_calls(self):
        with self.assertRaises(SystemExit) as cm:
            quiet(["--skills-dir", EXAMPLES, "--workspace", tempfile.mkdtemp(), "canary",
                   "--agent", "anthropic:claude-haiku-5-5", "--grader", "anthropic:claude-fable-5-1",
                   "--max-usd", "0.01"])
        self.assertIn("worst case", str(cm.exception))

    def test_canary_plan_has_agent_and_grader_parts(self):
        cases = [{"id": "a", "vector": "v", "context": "c" * 100, "task": "t", "pass_if": "p"}] * 2
        agent = behaviour.AnthropicAgent("claude-haiku-5-5", client=FakeClient())
        grader = behaviour.AnthropicGrader("claude-fable-5-1", client=FakeClient())
        plan = cli._canary_plan(cases, "body", agent, grader, reps=3)
        self.assertEqual([(m, n) for m, n, _, _ in plan], [("claude-haiku-5-5", 6), ("claude-fable-5-1", 2)])
        self.assertGreater(plan[1][2], 3 * agent.max_tokens, "grader input counts the replies it will read")
        regrade = cli._canary_plan(cases, "body", agent, grader, reps=3, replies=["no"] * 6)
        self.assertEqual([m for m, *_ in regrade], ["claude-fable-5-1"], "a regrade makes no agent calls")

    def test_effort_suffix_reaches_the_request(self):
        j = judges.make("anthropic:claude-opus-5-5@low")
        self.assertEqual(j.label, "anthropic:claude-opus-5-5@low")

    def test_unknown_agent_or_grader_kind_is_refused(self):
        for flag, spec in (("--agent", "anthropc:claude-haiku-5-5"), ("--grader", "nope:x")):
            with self.assertRaises(SystemExit) as cm:
                quiet(["--skills-dir", EXAMPLES, "--workspace", tempfile.mkdtemp(), "canary",
                       "--agent", "mutant:always-refuse", "--grader", "scripted", flag, spec])
            self.assertIn("unknown", str(cm.exception))

    def test_cli_backends_skip_the_api_preflight(self):
        ws = tempfile.mkdtemp()
        self.assertIn(quiet(["--skills-dir", EXAMPLES, "--workspace", ws, "trigger", "--judge", "scripted",
                             "--max-usd", "0.001", "--no-lint"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
