"""Offline tests for the workspace flag, the price table and spend cap, and the anthropic:
backend (docs/design-api-backend.md). No network and no SDK: the API is a fake client."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillevals import __main__ as cli  # noqa: E402
from skillevals import api, behaviour, judges, prices, report  # noqa: E402

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
        self.assertEqual(prices.estimate_tokens("abcde"), 3)  # ceil(5 bytes / 2)
        self.assertGreaterEqual(prices.estimate_tokens("日本"), 2)  # not ASCII: count bytes

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

    def test_any_other_stop_reason_is_an_error(self):
        for stop in ("pause_turn", "model_context_window_exceeded"):
            v = self.judge(FakeClient('{"skill": "eli5"}', stop_reason=stop))("S", "p", NAMES)
            self.assertEqual((v.picked, v.error), (None, f"stop:{stop}"))

    def test_test_guard_failures_are_not_swallowed(self):
        with self.assertRaises(AssertionError):
            self.judge(FakeClient(raises=AssertionError("guard")))("S", "p", NAMES)

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


class RecordingGrader:
    calls_per_case = 0
    label = "recording"
    cost_basis = "none"

    def __init__(self):
        self.seen = []

    def __call__(self, case, replies):
        self.seen += replies
        return [{"behaviour": "refuse", "meets_pass_if": True, "why": ""} for _ in replies], \
            behaviour.Reply("", 0.0)


class LongAgent:
    calls_per_reply = 1
    label = "long"
    cost_basis = "none"

    def __call__(self, system, prompt):
        return behaviour.Reply("y" * (behaviour.REPLY_CHARS + 500), 0.0)


class GradedText(unittest.TestCase):
    def test_grader_reads_exactly_the_stored_reply(self):
        cset = {"skill": "s", "canaries": [{"id": "a", "vector": "v", "expect": "refuse", "context": "c",
                                            "task": "t", "pass_if": "p"}]}
        g = RecordingGrader()
        rows = behaviour.run(cset, "body", LongAgent(), g, "2026-01-01T000000-x", reps=1, progress=lambda *a: None)
        self.assertEqual(g.seen, [rows[0]["reply"]])
        self.assertEqual(len(rows[0]["reply"]), behaviour.REPLY_CHARS)


class CostBasis(unittest.TestCase):
    def test_api_and_cli_costs_are_never_added(self):
        rows = [{"cost_usd": 1.0, "cost_basis": "api-usage"}, {"cost_usd": 2.0, "cost_basis": "cli-list"},
                {"cost_usd": 4.0}]  # rows written before cost_basis existed are CLI list price
        self.assertEqual(report.cost_by_basis(rows), {"api-usage": 1.0, "cli-list": 6.0})
        text = report.fmt_costs(report.cost_by_basis(rows))
        self.assertIn("$1.0000 real dollars (API)", text)
        self.assertIn("$6.0000 list price (CLI)", text)

    def test_rows_record_their_basis(self):
        j = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient('{"skill": "none"}'))
        sets = [{"skill": "eli5", "accept": [], "cases": [{"id": "x", "prompt": "p", "expect": "none"}]}]
        from skillevals import catalogue
        cat = catalogue.load(Path(EXAMPLES))
        rows = cli.trigger.run(sets, j, cat, "2026-01-01T000000-x", progress=lambda *a: None)
        self.assertEqual(rows[0]["cost_basis"], "api-usage")


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
        self.assertGreaterEqual(plan[1][2], 3 * prices.estimate_tokens("x" * behaviour.REPLY_CHARS),
                                "grader input counts the (cut) replies it will read")
        regrade = cli._canary_plan(cases, "body", agent, grader, reps=3, replies=["no"] * 6)
        self.assertEqual([m for m, *_ in regrade], ["claude-fable-5-1"], "a regrade makes no agent calls")

    def test_effort_suffix_reaches_the_request(self):
        j = judges.make("anthropic:claude-opus-5-5@low")
        self.assertEqual(j.label, "anthropic:claude-opus-5-5@low")
        j.call._client = c = FakeClient('{"skill": "none"}')
        j("S", "p", NAMES)
        self.assertEqual(c.requests[0]["output_config"]["effort"], "low")

    def test_cli_agent_with_api_grader_is_bounded_by_the_reply_cut(self):
        cases = [{"id": "a", "vector": "v", "context": "c", "task": "t", "pass_if": "p"}]
        grader = behaviour.AnthropicGrader("claude-fable-5-1", client=FakeClient())
        plan = cli._canary_plan(cases, "body", behaviour.ClaudeAgent("opus"), grader, reps=3)
        self.assertEqual(len(plan), 1)
        self.assertGreaterEqual(plan[0][2], 3 * prices.estimate_tokens("x" * behaviour.REPLY_CHARS))

    def test_trigger_plan_counts_the_schema(self):
        sets = [{"skill": "eli5", "cases": [{"prompt": "explain"}]}]
        judge = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient())
        args = Obj(samples=1)
        (_, _, inp, _), = cli._trigger_plan(args, sets, {}, judge, runs=1)
        floor = (prices.estimate_tokens(judges.ROUTER_SYSTEM.format(catalogue="")) + prices.estimate_tokens("explain")
                 + prices.estimate_tokens(json.dumps(judges.PICK_SCHEMA)))
        self.assertGreaterEqual(inp, floor)

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


AGENT_USAGE = {"input_tokens": 700, "output_tokens": 300,
               "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 0}
GRADER_USAGE = {"input_tokens": 2000, "output_tokens": 150,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900}
GRADES_TWO = ('{"grades": [{"n": 1, "behaviour": "refuse", "meets_pass_if": true, "why": "w"}, '
              '{"n": 2, "behaviour": "refuse", "meets_pass_if": true, "why": "w"}]}')
ONE_CASE = {"skill": "s", "canaries": [{"id": "a", "vector": "v", "expect": "refuse", "context": "c",
                                        "task": "t", "pass_if": "p"}]}


class TokenUsage(unittest.TestCase):
    """Every anthropic: row records the four token counts its call was billed for."""

    def test_judge_carries_all_four_counts(self):
        v = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient('{"skill": "eli5"}', usage=AGENT_USAGE))(
            "S", "p", NAMES)
        self.assertEqual(v.usage, AGENT_USAGE)

    def test_billed_error_keeps_its_usage(self):
        for kw in ({"stop_reason": "max_tokens"}, {"stop_reason": "refusal"}, {"stop_reason": "pause_turn"},
                   {"model": "claude-opus-5"}):
            v = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient(
                '{"skill": "el', usage=AGENT_USAGE, **kw))("S", "p", NAMES)
            self.assertTrue(v.error, kw)
            self.assertEqual(v.usage, AGENT_USAGE, kw)

    def test_missing_count_is_stored_as_zero(self):
        v = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient(
            '{"skill": "eli5"}', usage=dict(AGENT_USAGE, cache_read_input_tokens=None)))("S", "p", NAMES)
        self.assertEqual(v.usage["cache_read_input_tokens"], 0)

    def test_failed_request_has_no_usage(self):
        v = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient(raises=RuntimeError("x")))("S", "p", NAMES)
        self.assertIsNone(v.usage)

    def test_trigger_row_records_usage(self):
        j = judges.AnthropicJudge("claude-haiku-5-5", client=FakeClient('{"skill": "none"}', usage=AGENT_USAGE))
        sets = [{"skill": "eli5", "accept": [], "cases": [{"id": "x", "prompt": "p", "expect": "none"}]}]
        from skillevals import catalogue
        rows = cli.trigger.run(sets, j, catalogue.load(Path(EXAMPLES)), "2026-01-01T000000-x",
                               progress=lambda *a: None)
        self.assertEqual(rows[0]["usage"], AGENT_USAGE)

    def test_non_api_trigger_row_has_null_usage(self):
        from skillevals import catalogue
        sets = [{"skill": "eli5", "accept": [], "cases": [{"id": "x", "prompt": "p", "expect": "none"}]}]
        rows = cli.trigger.run(sets, judges.make("scripted"), catalogue.load(Path(EXAMPLES)),
                               "2026-01-01T000000-x", progress=lambda *a: None)
        self.assertIsNone(rows[0]["usage"])

    def test_canary_rows_record_agent_and_grader_usage(self):
        agent = behaviour.AnthropicAgent("claude-sonnet-5-5", client=FakeClient("I will not.", usage=AGENT_USAGE))
        grader = behaviour.AnthropicGrader("claude-fable-5-1", client=FakeClient(GRADES_TWO, usage=GRADER_USAGE))
        rows = behaviour.run(ONE_CASE, "body", agent, grader, "2026-01-01T000000-x", reps=2,
                             progress=lambda *a: None)
        self.assertEqual([r["agent_usage"] for r in rows], [AGENT_USAGE, AGENT_USAGE])
        self.assertEqual([r["grader_usage"] for r in rows], [GRADER_USAGE, None],
                         "one grader call per case: its usage sits on the first row, like its cost")

    def test_regrade_records_grader_usage_and_no_agent_usage(self):
        old = [{"run_id": "r0", "case_id": "a", "reply": "no", "error": None, "agent_usage": AGENT_USAGE}] * 2
        grader = behaviour.AnthropicGrader("claude-opus-5-5", client=FakeClient(GRADES_TWO, usage=GRADER_USAGE))
        new = behaviour.regrade(old, ONE_CASE, grader, "r1")
        self.assertEqual([r["grader_usage"] for r in new], [GRADER_USAGE, None])
        self.assertEqual([r["agent_usage"] for r in new], [None, None])



class CountingGrader(behaviour.ScriptedGrader):
    def __init__(self):
        self.calls = 0

    def __call__(self, case, replies):
        self.calls += 1
        return super().__call__(case, replies)


class Regrade(unittest.TestCase):
    def canary_file(self, ws: str) -> Path:
        quiet(["--skills-dir", EXAMPLES, "--workspace", ws, "canary", "--agent", "mutant:always-refuse",
               "--grader", "scripted"])
        f, = Path(ws).glob("results/canary/*/*.jsonl")
        return f

    def test_case_with_no_graded_reply_makes_no_grader_call(self):
        rows = [{"run_id": "r0", "case_id": "a", "reply": "", "error": "refusal:cyber"},
                {"run_id": "r0", "case_id": "b", "reply": "no", "error": None}]
        cset = {"skill": "s", "canaries": [dict(ONE_CASE["canaries"][0], id=i) for i in ("a", "b")]}
        g = CountingGrader()
        new = behaviour.regrade(rows, cset, g, "r1")
        self.assertEqual(g.calls, 1, "case a had only errored rows: nothing to grade, nothing to bill")
        self.assertEqual([r["case_id"] for r in new], ["b"])

    def test_regrade_file_is_named_after_the_source_agent_not_the_flag(self):
        ws = tempfile.mkdtemp()
        f = self.canary_file(ws)
        rows = [dict(json.loads(l), agent="anthropic:claude-opus-5-5@medium") for l in f.read_text().splitlines()]
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        quiet(["--skills-dir", EXAMPLES, "--workspace", ws, "canary", "--regrade", str(f), "--grader", "scripted",
               "--agent", "anthropic:claude-sonnet-5-5"])
        out = [p.name for p in Path(ws).glob("results/canary/*/*regrade*.jsonl")]
        self.assertEqual(len(out), 1)
        self.assertIn("claude-opus-5-5", out[0])
        self.assertNotIn("sonnet", out[0])

    def test_regrade_refuses_a_file_with_two_agents(self):
        ws = tempfile.mkdtemp()
        f = self.canary_file(ws)
        lines = f.read_text().splitlines()
        rows = [dict(json.loads(l), agent=("a" if i % 2 else "b")) for i, l in enumerate(lines)]
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        with self.assertRaises(SystemExit) as cm:
            quiet(["--skills-dir", EXAMPLES, "--workspace", ws, "canary", "--regrade", str(f), "--grader", "scripted"])
        self.assertIn("agent", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
