"""Unit tests for the canary regression gate. Offline: synthetic rows, scripted agents and graders.

Every check has a control that shows it CAN fail.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillevals import behaviour, gate  # noqa: E402

SKILL, AGENT, SHA = "demo", "claude-cli:sonnet", "aaaaaaaaaaaa"
BASE = "2026-01-01T000000-canary-claude-cli-sonnet"
CAND = "2026-01-02T000000-canary-claude-cli-sonnet"
CASES = [("c1", "refuse"), ("c1c", "comply")]


def rows(run_id, passed, regraded_from=None, sha=SHA, agent=AGENT, errors=()):
    """One row per (case, rep); `passed` is a list of booleans in case-then-rep order."""
    out, i = [], 0
    for cid, expect in CASES:
        for rep in range(len(passed) // len(CASES)):
            p, err = passed[i], i in errors
            out.append({"run_id": run_id, "date": run_id[:10], "skill": SKILL, "case_id": cid, "rep": rep,
                        "expect": expect, "agent": agent, "skill_sha": sha, "regraded_from": regraded_from,
                        "passed": None if err else p, "error": "grader: unparseable grade" if err else None})
            i += 1
    return out


def write(root: Path, rs: list[dict]) -> None:
    d = root / "results" / "canary" / rs[0]["date"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rs[0]['run_id']}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs), encoding="utf-8")


def regrade_id(run_id, n=1):
    return run_id.replace("-canary-", "-canary-regrade-") + f"-{n}"


class Gate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sev-gate-"))
        ok = [True] * 4
        write(self.root, rows(BASE, ok))
        write(self.root, rows(regrade_id(BASE), ok, regraded_from=BASE))

    def run_gate(self, sha=SHA):
        return gate.check(self.root, SKILL, BASE, sha)

    def add_candidate(self, g1, g2=None, **kw):
        write(self.root, rows(CAND, g1, **kw))
        if g2 is not None:
            write(self.root, rows(regrade_id(CAND), g2, regraded_from=CAND))

    def test_certified_candidate_passes(self):
        self.add_candidate([True] * 4, [True] * 4)
        self.assertEqual(self.run_gate()[0], 0)

    def test_fail_under_both_gradings_is_a_regression(self):
        self.add_candidate([True, False, True, True], [True, False, True, True])
        code, lines = self.run_gate()
        self.assertEqual(code, 1)
        self.assertTrue(any("REGRESSION c1 rep1" in ln for ln in lines))

    def test_disagreeing_gradings_do_not_fail_the_gate(self):
        self.add_candidate([True, False, True, True], [True, True, True, True])
        code, lines = self.run_gate()
        self.assertEqual(code, 0)
        self.assertTrue(any("disputed   c1 rep1" in ln for ln in lines))

    def test_one_grading_is_not_enough(self):
        self.add_candidate([True, False, True, True])          # no regrade: the fail is unconfirmed
        self.assertEqual(self.run_gate()[0], 2)
        # an errored grading does not count as a second grading
        self.add_candidate([True] * 4, [True] * 4)
        write(self.root, rows(regrade_id(CAND), [True] * 4, regraded_from=CAND, errors=(0,)))
        self.assertEqual(self.run_gate()[0], 2)

    def test_a_later_good_run_does_not_hide_a_bad_one(self):
        self.add_candidate([False, True, True, True], [False, True, True, True])
        later = "2026-01-03T000000-canary-claude-cli-sonnet"
        write(self.root, rows(later, [True] * 4))
        write(self.root, rows(regrade_id(later), [True] * 4, regraded_from=later))
        self.assertEqual(self.run_gate()[0], 1)

    def test_runs_before_the_baseline_and_other_agents_are_ignored(self):
        old = "2025-12-31T000000-canary-claude-cli-sonnet"
        write(self.root, rows(old, [False] * 4))
        write(self.root, rows(regrade_id(old), [False] * 4, regraded_from=old))
        mutant = "2026-01-02T000001-canary-mutant-always-refuse"
        write(self.root, rows(mutant, [False] * 4, agent="mutant:always-refuse"))
        write(self.root, rows(regrade_id(mutant), [False] * 4, regraded_from=mutant, agent="mutant:always-refuse"))
        self.assertEqual(self.run_gate()[0], 0)
        # control: the same failing rows from the gated agent after the baseline do fail it
        self.add_candidate([False] * 4, [False] * 4)
        self.assertEqual(self.run_gate()[0], 1)

    def test_uncertified_baseline_is_inconclusive(self):
        write(self.root, rows(regrade_id(BASE), [True, True, False, True], regraded_from=BASE))
        self.assertEqual(self.run_gate()[0], 2)

    def test_missing_baseline_is_inconclusive(self):
        self.assertEqual(gate.check(self.root, SKILL, "2026-01-01T000009-canary-nope", SHA)[0], 2)

    def test_changed_skill_file_needs_a_rerun(self):
        self.add_candidate([True] * 4, [True] * 4)
        self.assertEqual(self.run_gate(sha="bbbbbbbbbbbb")[0], 2)

    def test_candidate_missing_rows_is_inconclusive(self):
        rs = rows(CAND, [True] * 4)[:2]                       # only c1
        write(self.root, rs)
        write(self.root, [dict(r, run_id=regrade_id(CAND), regraded_from=CAND) for r in rs])
        self.assertEqual(self.run_gate()[0], 2)


CANARY_SET = {"skill": SKILL, "canaries": [
    {"id": "c1", "vector": "peer-message", "expect": "refuse", "context": "x", "task": "t", "pass_if": "p"},
    {"id": "c1c", "vector": "peer-message-control", "expect": "comply", "context": "x", "task": "t", "pass_if": "p"},
]}


class GoodGrader:
    """Grades every reply as the case expects: a working grader on a well-behaved agent."""
    calls_per_case, label = 0, "scripted:good"

    def __call__(self, case, replies):
        return [{"behaviour": case["expect"], "meets_pass_if": True, "why": "ok"} for _ in replies], \
            behaviour.Reply("", 0.0)


class BrokenGrader(GoodGrader):
    """A grader whose pass_if check is broken: it says no reply meets pass_if."""
    label = "scripted:broken"

    def __call__(self, case, replies):
        g, r = super().__call__(case, replies)
        return [dict(e, meets_pass_if=False) for e in g], r


class GateEndToEnd(unittest.TestCase):
    """The canary pipeline (run, regrade, write) feeding the gate, with no model call."""

    def pipeline(self, root: Path, run_id: str, grader) -> None:
        agent = behaviour.MutantAgent("always-refuse")
        agent.label = AGENT
        rs = behaviour.run(CANARY_SET, "body", agent, grader, run_id, reps=2,
                           progress=lambda *_: None, workers=1)
        for r in rs:
            r["skill_sha"] = SHA
        behaviour.write_jsonl(rs, root)
        behaviour.write_jsonl(behaviour.regrade(rs, CANARY_SET, grader, regrade_id(run_id)), root)

    def test_broken_grader_fails_the_gate(self):
        root = Path(tempfile.mkdtemp(prefix="sev-gate-e2e-"))
        self.pipeline(root, BASE, GoodGrader())
        self.pipeline(root, CAND, BrokenGrader())
        code, lines = gate.check(root, SKILL, BASE, SHA)
        self.assertEqual(code, 1, lines)
        # control: the same pipeline with the working grader passes
        root2 = Path(tempfile.mkdtemp(prefix="sev-gate-e2e-"))
        self.pipeline(root2, BASE, GoodGrader())
        self.pipeline(root2, CAND, GoodGrader())
        self.assertEqual(gate.check(root2, SKILL, BASE, SHA)[0], 0)


if __name__ == "__main__":
    unittest.main()
