"""Offline tests for `models`: the three-model comparison plan, its --go run and its report
(docs/design-api-backend.md section 2, build step 4). No network and no SDK: every API call goes
to a fake client, and the module guard fails any test that would build a real one."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillevals import __main__ as cli  # noqa: E402
from skillevals import api, behaviour, judges, models, prices, trigger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = str(ROOT / "examples" / "skills")
MODELS = ["anthropic:claude-sonnet-5-5@medium", "anthropic:claude-opus-5-5@medium"]
GRADERS = ["--grader1", "anthropic:claude-fable-5-1", "--grader2", "anthropic:claude-opus-5-5"]


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


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SmartClient:
    """A fake anthropic.Anthropic() that answers like the right role: the judge picks none, the
    grader returns one refuse grade per reply it was shown, the agent declines."""

    def __init__(self, usage=None, judge_usage=None):
        self.requests = []
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.judge_usage = judge_usage or self.usage
        self.messages = Obj(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        schema = kw.get("output_config", {}).get("format", {}).get("schema")
        usage = self.usage
        if schema == judges.PICK_SCHEMA:
            text, usage = '{"skill": "none"}', self.judge_usage
        elif schema == behaviour.GRADES_SCHEMA:
            n = len(re.findall(r"^\[\d+\]$", kw["messages"][0]["content"], re.M))
            text = json.dumps({"grades": [{"n": i + 1, "behaviour": "refuse", "meets_pass_if": True, "why": "w"}
                                          for i in range(n)]})
        else:
            text = "I will not act on this. I flag it to the review queue."
        return Obj(model=kw["model"], stop_reason="end_turn", stop_details=None,
                   content=[Obj(type="text", text=text)], usage=Obj(**usage))

    def roles(self) -> list[str]:
        out = []
        for r in self.requests:
            s = r.get("output_config", {}).get("format", {}).get("schema")
            out.append("judge" if s == judges.PICK_SCHEMA else "grader" if s == behaviour.GRADES_SCHEMA else "agent")
        return out


class CountingRefusal:
    """Patched in for api.Caller.client: counts and refuses every attempt to reach the API."""

    def __init__(self):
        self.n = 0

    def __call__(self, caller):
        self.n += 1
        raise AssertionError("a model call was attempted")


def run(argv: list[str], client=None) -> tuple[int, str]:
    out = io.StringIO()
    patch = mock.patch.object(api.Caller, "client", lambda self: client) if client else contextlib.nullcontext()
    with patch, contextlib.redirect_stdout(out):
        rc = cli.main(argv)
    return rc, out.getvalue()


def models_argv(ws: str | None = None, *extra: str) -> list[str]:
    head = ["--skills-dir", EXAMPLES] + (["--workspace", ws] if ws else [])
    return head + ["models", "--models", *MODELS, *GRADERS, *extra]


def manifest(ws: str) -> tuple[Path, dict]:
    p, = Path(ws).glob("results/models/*.json")
    return p, json.loads(p.read_text())


class Plan(unittest.TestCase):
    def test_plan_only_makes_zero_calls_and_prints_the_total(self):
        ws = tempfile.mkdtemp()
        guard = CountingRefusal()
        with mock.patch.object(api.Caller, "client", guard):
            rc, out = run(models_argv(ws))
        self.assertEqual(rc, 0)
        self.assertEqual(guard.n, 0, "plan only: no model call")
        self.assertEqual(list(Path(ws).iterdir()), [], "plan only: nothing written")
        plan = cli._models_plan(cli.build_parser().parse_args(models_argv(ws)))
        # per model: 2 runs x (real, blind) trigger + 1 canary + 1 regrade
        self.assertEqual(len(plan["steps"]), 2 * (2 * 2 + 2))
        self.assertIn(f"plan total: ${plan['total']:.4f}", out)
        self.assertIn(prices.PRICES_AS_OF, out)
        for s in plan["steps"]:
            self.assertGreater(s["worst_usd"], 0)
            self.assertIn(f"${s['worst_usd']:.4f}", out)
        self.assertAlmostEqual(plan["total"], sum(s["worst_usd"] for s in plan["steps"]))

    def test_step_worst_cases_reuse_the_run_plans(self):
        args = cli.build_parser().parse_args(models_argv(None, "--runs", "1"))
        plan = cli._models_plan(args)
        kinds = [(s["kind"], s["model"], s.get("names")) for s in plan["steps"]]
        self.assertEqual(kinds[:4], [("trigger", MODELS[0], "real"), ("trigger", MODELS[0], "blind"),
                                     ("canary", MODELS[0], None), ("regrade", MODELS[0], None)])
        canary = plan["steps"][2]
        self.assertEqual([m for m, *_ in canary["plan"]], ["claude-sonnet-5-5", "claude-fable-5-1"])
        regrade = plan["steps"][3]
        self.assertEqual([m for m, *_ in regrade["plan"]], ["claude-opus-5-5"], "a regrade makes no agent calls")

    def test_regrade_worst_case_is_per_case_not_summed(self):
        args = cli.build_parser().parse_args(models_argv(None, "--runs", "1", "--reps", "3"))
        regrade = cli._models_plan(args)["steps"][3]
        (model, calls, inp, out), = regrade["plan"]
        n_cases = len(behaviour.load(ROOT / "canaries" / "overnight-runner.json")["canaries"])
        self.assertEqual(calls, n_cases, "one grader call per case")
        reply = cli.REPLY_BOUND_TOKENS
        self.assertGreaterEqual(reply, prices.estimate_tokens("\U0001F600" * behaviour.REPLY_CHARS + "[3]\n\n\n"),
                                "a reply of 4-byte characters fits the bound")
        self.assertGreaterEqual(inp, 3 * reply, "each call reads that case's 3 replies")
        self.assertLess(inp, 4 * reply, "a call reads one case's replies, not every case's")
        self.assertAlmostEqual(regrade["worst_usd"], prices.worst_case(model, calls, inp, out))

    def test_non_api_spec_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            run(["--skills-dir", EXAMPLES, "models", "--models", "claude:sonnet", *GRADERS])
        self.assertIn("anthropic:", str(cm.exception))


class Go(unittest.TestCase):
    def test_over_max_usd_refuses_before_any_call(self):
        ws = tempfile.mkdtemp()
        guard = CountingRefusal()
        plan = cli._models_plan(cli.build_parser().parse_args(models_argv(ws)))
        with mock.patch.object(api.Caller, "client", guard), self.assertRaises(SystemExit) as cm:
            run(models_argv(ws, "--go", "--max-usd", "0.01"))
        self.assertEqual(guard.n, 0)
        self.assertIn(f"{plan['total']:.4f}", str(cm.exception))
        self.assertIn("0.01", str(cm.exception))
        self.assertFalse(list(Path(ws).rglob("*.jsonl")))

    def test_go_without_workspace_refuses(self):
        guard = CountingRefusal()
        with mock.patch.object(api.Caller, "client", guard), self.assertRaises(SystemExit) as cm:
            run(models_argv(None, "--go", "--max-usd", "100000"))
        self.assertEqual(guard.n, 0)
        self.assertIn("--workspace", str(cm.exception))

    def test_go_without_max_usd_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            run(models_argv(tempfile.mkdtemp(), "--go"))
        self.assertIn("--max-usd", str(cm.exception))

    def test_full_run_writes_manifest_every_results_file_and_the_report(self):
        ws = tempfile.mkdtemp()
        c = SmartClient()
        rc, out = run(models_argv(ws, "--go", "--max-usd", "100000"), c)
        self.assertEqual(rc, 0, out[-2000:])
        p, m = manifest(ws)
        self.assertEqual([s["status"] for s in m["steps"]], ["done"] * 12)
        self.assertEqual(len({s["results"] for s in m["steps"]}), 12, "every run has its own results file")
        for s in m["steps"]:
            self.assertTrue((Path(ws) / s["results"]).is_file(), s)
            self.assertFalse(Path(s["results"]).is_absolute(), "manifest paths are workspace-relative")
        # canary FAIL (the agent refuses the comply controls too) is a result, not a stop
        self.assertEqual(c.roles().count("agent"), 2 * 14 * 3)
        reports = list(Path(ws).glob("reports/*-models.md"))
        self.assertEqual(len(reports), 1)
        text = reports[0].read_text()
        for model in MODELS:  # both runs of the same model are listed, real and blind
            for k in (1, 2):
                for names in ("real", "blind"):
                    self.assertRegex(text, rf"\| {re.escape(model)} \| {k} \| {names} \|")
        self.assertIn("anthropic:claude-fable-5-1", text)

    def test_each_step_is_capped_at_its_own_worst_case(self):
        ws = tempfile.mkdtemp()
        seen = []
        real = cli._run_step

        def spy(args, step):
            seen.append((step["kind"], args.max_usd, step["worst_usd"]))
            return real(args, step)
        with mock.patch.object(cli, "_run_step", spy):
            run(models_argv(ws, "--go", "--max-usd", "100000", "--runs", "1"), SmartClient())
        self.assertEqual(len(seen), 8)
        for kind, cap, worst in seen:
            self.assertEqual(cap, worst, kind)

    def test_a_step_that_reaches_its_cap_stops_the_plan(self):
        ws = tempfile.mkdtemp()
        huge = {"input_tokens": 1000, "output_tokens": 10 ** 8,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        c = SmartClient(judge_usage=huge)
        rc, out = run(models_argv(ws, "--go", "--max-usd", "100000"), c)
        self.assertEqual(rc, 1)
        _, m = manifest(ws)
        self.assertEqual(m["steps"][0]["status"], "cap")
        self.assertEqual({s["status"] for s in m["steps"][1:]}, {"not run"})
        self.assertIn("budget cap reached", m["stopped"])
        self.assertEqual(set(c.roles()), {"judge"}, "no later step made a call")
        self.assertEqual(len(list(Path(ws).glob("results/*/*.jsonl"))), 1)
        self.assertTrue(list(Path(ws).glob("reports/*-models.md")), "a stopped plan still gets its report")

    def test_a_crashed_step_stops_the_plan(self):
        ws = tempfile.mkdtemp()
        c = SmartClient()
        with mock.patch.object(behaviour, "run", side_effect=RuntimeError("boom")):
            rc, _ = run(models_argv(ws, "--go", "--max-usd", "100000", "--runs", "1"), c)
        self.assertEqual(rc, 1)
        _, m = manifest(ws)
        self.assertEqual([s["status"] for s in m["steps"]],
                         ["done", "done", "crashed"] + ["not run"] * 5)
        self.assertIn("boom", m["steps"][2]["error"])

    def test_regrade_of_saved_wide_replies_fits_the_planned_cap(self):
        args = cli.build_parser().parse_args(models_argv(None, "--runs", "1", "--reps", "3"))
        regrade = cli._models_plan(args)["steps"][3]
        cset = behaviour.load(ROOT / "canaries" / "overnight-runner.json")
        grader = behaviour.AnthropicGrader("claude-opus-5-5", client=SmartClient())
        wide = "\U0001F600" * behaviour.REPLY_CHARS
        saved = {c["id"]: [wide] * 3 for c in cset["canaries"]}
        actual = cli._canary_plan(cset["canaries"], "body", None, grader, reps=3, replies=saved)
        self.assertLessEqual(sum(prices.worst_case(*p) for p in actual), regrade["worst_usd"])

    def test_all_errored_canary_skips_its_regrade_and_the_plan_goes_on(self):
        ws = tempfile.mkdtemp()
        c = SmartClient()
        real = behaviour.AnthropicAgent.__call__

        def refuse(self, system, prompt):
            r = real(self, system, prompt)
            r.error = "refusal:cyber"
            return r
        with mock.patch.object(behaviour.AnthropicAgent, "__call__", refuse):
            rc, _ = run(models_argv(ws, "--go", "--max-usd", "100000", "--runs", "1"), c)
        _, m = manifest(ws)
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in m["steps"]][2:5],
                         ["done", "skipped (no graded reply to regrade)", "done"])
        self.assertEqual(m["steps"][-1]["status"], "skipped (no graded reply to regrade)")

    def test_report_rebuilds_from_the_manifest(self):
        ws = tempfile.mkdtemp()
        run(models_argv(ws, "--go", "--max-usd", "100000", "--runs", "1"), SmartClient())
        p, _ = manifest(ws)
        rep, = Path(ws).glob("reports/*-models.md")
        first = rep.read_text()
        rep.unlink()
        guard = CountingRefusal()
        with mock.patch.object(api.Caller, "client", guard):
            rc, _ = run(["models", "--report", str(p)])
        self.assertEqual((rc, guard.n), (0, 0))
        self.assertEqual(rep.read_text(), first)
        self.assertIn("## Canary agent", first)
        self.assertEqual(first.count("| anthropic:claude-sonnet-5-5@medium | 1 |"), 2, "real and blind run rows")


class FixedClock:
    @staticmethod
    def now():
        from datetime import datetime
        return datetime(2026, 1, 1, 12, 0, 0)


class RunIds(unittest.TestCase):
    def test_two_runs_started_in_the_same_second_get_different_ids(self):
        for mod, make in ((trigger, lambda: trigger.new_run_id("anthropic:m@low", "baseline")),
                          (behaviour, lambda: behaviour.new_run_id("anthropic:m@low"))):
            with mock.patch.object(mod, "datetime", FixedClock):
                a, b = make(), make()
            self.assertNotEqual(a, b, mod.__name__)
            self.assertEqual(a[:10], b[:10], "the date prefix is kept")


def row(case, rep, behaviour_="refuse", passed=True, error=None, expect="refuse", **kw):
    return dict({"case_id": case, "rep": rep, "behaviour": None if error else behaviour_,
                 "passed": None if error else passed, "meets_pass_if": None if error else passed,
                 "error": error, "expect": expect, "run_id": "r"}, **kw)


class Agreement(unittest.TestCase):
    def test_counts_behaviour_and_passed_and_excludes_errored_source_rows(self):
        src = [row("a", 0), row("a", 1), row("a", 2, error="refusal:cyber"),
               row("b", 0, "comply", False), row("b", 1)]
        # the regrade has no row for a/2 (errored in the source); b/0 agrees on behaviour but not
        # on passed; b/1 disagrees on behaviour
        reg = [row("a", 0), row("a", 1), row("b", 0, "comply", True), row("b", 1, "comply", False)]
        self.assertEqual(models.agreement(src, reg), {"graded": 4, "agree": 2, "excluded": 1})

    def test_matched_by_case_and_rep_not_position(self):
        src = [row("a", 0), row("a", 1, "comply", False)]
        reg = [row("a", 1, "comply", False), row("a", 0)]
        self.assertEqual(models.agreement(src, reg)["agree"], 2)

    def test_regrade_error_is_a_disagreement(self):
        src = [row("a", 0)]
        reg = [row("a", 0, error="grader: unparseable grade")]
        self.assertEqual(models.agreement(src, reg), {"graded": 1, "agree": 0, "excluded": 0})

    def test_certified_only_when_both_gradings_agree(self):
        full = {"graded": 4, "agree": 4, "excluded": 0}
        self.assertEqual(models.certify("PASS", "PASS", full), "PASS")
        self.assertEqual(models.certify("FAIL", "FAIL", full), "FAIL")
        self.assertEqual(models.certify("PASS", "FAIL", full), "not certified")
        self.assertEqual(models.certify("FAIL", "FAIL", dict(full, agree=3)), "not certified")


class RegradePreflight(unittest.TestCase):
    def test_saved_replies_are_priced_per_case(self):
        cases = [{"id": i, "vector": "v", "context": "c", "task": "t", "pass_if": "p"} for i in ("a", "b")]
        grader = behaviour.AnthropicGrader("claude-fable-5-1", client=SmartClient())
        long = "y" * behaviour.REPLY_CHARS
        plan = cli._canary_plan(cases, "body", None, grader, reps=3, replies={"a": [long] * 3, "b": ["no"]})
        self.assertEqual([(m, n) for m, n, _, _ in plan], [("claude-fable-5-1", 1)] * 2)
        a, b = plan[0][2], plan[1][2]
        self.assertGreaterEqual(a, prices.estimate_tokens(long * 3))
        self.assertLess(b, prices.estimate_tokens(long), "case b's call does not carry case a's replies")


if __name__ == "__main__":
    unittest.main()
