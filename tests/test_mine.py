"""Offline tests for the transcript miner, the scrub check, the local scrub and accept
(docs/design-api-backend.md section 4). Fixtures are synthetic. No model is called: the scrub
model is a fake, and any real HTTP call fails the test."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillevals import __main__ as cli  # noqa: E402
from skillevals import mine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "transcripts"
EXAMPLES = str(ROOT / "examples" / "skills")


def setUpModule():
    """No test here may reach a real server: any urlopen fails the test."""
    def blocked(*a, **kw):
        raise AssertionError("a test tried to open a real HTTP connection")
    global _saved
    _saved = (urllib.request.urlopen, urllib.request.OpenerDirector.open)
    urllib.request.urlopen = blocked
    urllib.request.OpenerDirector.open = blocked


def tearDownModule():
    urllib.request.urlopen, urllib.request.OpenerDirector.open = _saved


def tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and ".git" not in rel.parts and "__pycache__" not in rel.parts:
            out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def mined(**kw):
    return mine.mine(FIX, {"eli5", "gen-commits", "handoff", "wrap"}, **kw)


class Miner(unittest.TestCase):
    def test_each_signal_is_found(self):
        hits, _ = mined()
        got = {(h["signal"], h["skill"], h["prompt"]) for h in hits}
        self.assertIn(("corrected", "gen-commits", "can you explain the retry loop in plain words"), got)
        self.assertIn(("missed", "gen-commits", "summarize the staged changes as a commit message"), got)
        self.assertIn(("interrupted", "handoff", "write a note so the next session can pick this up"), got)
        self.assertIn(("corrected", "handoff", "explain the cache thing simply"), got)
        self.assertIn(("corrected", "handoff", "explain what this regex does"), got)  # A fired, user typed /B
        self.assertIn(("interrupted", "wrap", "draft the weekly summary"), got)
        self.assertIn(("missed", "gen-commits", "ok keep going"), got)
        self.assertEqual(len(hits), 7, [(h["signal"], h["prompt"]) for h in hits])

    def test_hit_keeps_source_line_and_timestamp(self):
        hits, _ = mined()
        h = next(h for h in hits if h["signal"] == "missed")
        self.assertEqual(h["source"], "proj-a/session-a.jsonl")
        self.assertEqual(h["line"], 7)  # the typed prompt's line, 1-based
        self.assertEqual(h["timestamp"], "2026-10-01T10:00:06.000Z")
        self.assertEqual(h["evidence_line"], 9)  # the /gen-commits line

    def test_near_misses_are_not_flagged(self):
        hits, _ = mined()
        prompts = {h["prompt"] for h in hits}
        self.assertNotIn("explain this diff like I'm five", prompts)        # "no worries" is thanks
        self.assertNotIn("tidy up the context please", prompts)             # /clear is not a skill
        self.assertNotIn("make the commit message for these changes", prompts)  # it fired, then typed

    def test_correction_patterns(self):
        for t in ["no, wrong skill", "No.", "nope", "not that one", "I meant the other thing",
                  "wrong skill", "that's not what I asked"]:
            self.assertTrue(mine.is_correction(t), t)
        for t in ["no worries", "no problem at all", "knowledge base please", "noted, thanks",
                  "nothing else", "now do the next one"]:
            self.assertFalse(mine.is_correction(t), t)

    def test_sidechain_and_tool_output_never_become_prompts(self):
        hits, stats = mined()
        for h in hits:
            self.assertNotIn("SIDECHAIN", h["prompt"])
            self.assertNotIn("TOOLOUT", h["prompt"])
        # the tool_result "no, wrong skill" after wrap did not count as a correction
        self.assertFalse([h for h in hits if h["skill"] == "wrap" and h["source"].startswith("proj-b/")])
        # the tool_result holding /gen-commits did not count as a by-hand invocation
        self.assertNotIn("summarize the staged changes again", {h["prompt"] for h in hits})
        self.assertEqual(stats["sidechain"], 3)

    def test_malformed_lines_counted_not_fatal(self):
        _, stats = mined()
        self.assertEqual(stats["malformed"], 3)  # bad JSON, a JSON list, a user line with no message
        self.assertEqual(stats["other_types"], 2)  # mode, api-request
        self.assertEqual(stats["files"], 3)

    def test_since_filters_by_prompt_date(self):
        hits, _ = mined(since="2026-09-15")
        self.assertEqual(len(hits), 6)
        self.assertNotIn("explain the cache thing simply", {h["prompt"] for h in hits})

    def test_unknown_skill_by_hand_is_ignored(self):
        hits, _ = mine.mine(FIX, {"eli5", "handoff", "wrap"})  # gen-commits not installed
        self.assertNotIn("missed", {h["signal"] for h in hits})


    def test_since_includes_the_boundary_date(self):
        hits, _ = mined(since="2026-10-03")
        self.assertEqual({h["timestamp"][:10] for h in hits}, {"2026-10-03"})
        self.assertEqual(len(hits), 3)

    def test_compact_summary_and_transcript_only_lines_skipped(self):
        hits, stats = mined()
        prompts = {h["prompt"] for h in hits}
        self.assertNotIn("check the flaky test list", prompts)
        self.assertNotIn("count the open tickets", prompts)
        self.assertEqual(stats["compact_or_transcript_only"], 2)

    def test_meta_and_harness_text_skipped(self):
        hits, stats = mined()
        prompts = {h["prompt"] for h in hits}
        self.assertNotIn("draft the release blurb", prompts)   # isMeta "no, ..." is not a correction
        self.assertNotIn("tidy the imports", prompts)          # <system-reminder> is not a correction
        self.assertFalse([p for p in prompts if p.startswith("<")])
        self.assertEqual(stats["meta"], 1)
        self.assertEqual(stats["harness_text"], 2)

    def test_reroute_to_another_skill_is_a_correction_same_skill_is_not(self):
        hits, _ = mined()
        h = next(h for h in hits if h["prompt"] == "explain what this regex does")
        self.assertEqual((h["signal"], h["skill"], h.get("rerouted_to")), ("corrected", "handoff", "eli5"))
        self.assertNotIn("make the commit message for these changes", {h["prompt"] for h in hits})

    def test_interrupt_ends_the_run(self):
        hits, _ = mined()
        self.assertEqual([h["signal"] for h in hits if h["prompt"] == "draft the weekly summary"], ["interrupted"])
        self.assertNotIn("no, I meant the journal", {h["prompt"] for h in hits})

    def test_interrupt_marker_counts_only_as_a_prefix(self):
        hits, _ = mined()
        self.assertNotIn("summarize this file for me", {h["prompt"] for h in hits})

    def test_by_hand_command_counts_only_as_the_next_typed_turn(self):
        hits, _ = mined()
        self.assertNotIn("summarize the staged changes for review", {h["prompt"] for h in hits})

class MineCli(unittest.TestCase):
    def test_mine_without_workspace_refuses(self):
        with self.assertRaises(SystemExit) as e:
            run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX)])
        self.assertIn("workspace", str(e.exception))

    def test_mine_refuses_a_workspace_inside_the_repo(self):
        for ws in (ROOT, ROOT / "results" / "private"):
            with self.assertRaises(SystemExit) as e:
                run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX), "--workspace", str(ws)])
            self.assertIn("inside the repo", str(e.exception))
        self.assertFalse((ROOT / "results" / "private").exists())

    def test_mine_writes_hits_to_the_workspace_and_prints_counts(self):
        before = tree_digest(ROOT)
        ws = Path(tempfile.mkdtemp())
        code, out = run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX), "--workspace", str(ws)])
        self.assertEqual(code, 0)
        files = list((ws / "mined").glob("hits-*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(read_jsonl(files[0])), 7)
        for word in ("corrected 3", "missed 2", "interrupted 2", "malformed 3"):
            self.assertIn(word, out)
        self.assertEqual(tree_digest(ROOT), before)

    def test_workspace_before_the_subcommand_also_works(self):
        ws = Path(tempfile.mkdtemp())
        code, _ = run(["--skills-dir", EXAMPLES, "--workspace", str(ws), "mine", "--transcripts", str(FIX)])
        self.assertEqual(code, 0)
        self.assertTrue(list((ws / "mined").glob("hits-*.jsonl")))


class ScrubCheck(unittest.TestCase):
    # every "secret" below is made up and split so repo secret scanners do not flag the source
    CASES = {
        "secret:anthropic-key": "use key " + "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz0123 for this",
        "secret:aws-access-key": "creds " + "AKIA" + "ABCDEFGHIJKLMNOP in the env",
        "secret:private-key": "-----BEGIN OPENSSH " + "PRIVATE KEY----- blah",
        "secret:github-token": "token " + "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "secret:assignment": "set password=hunter2hunter2 then retry",
        "deny:email": "send it to someone@example.org today",
        "deny:url": "read https://example.org/page first",
        "deny:hostname": "deploy to build.internal-box.io tonight",
        "deny:ip-address": "ssh to 10.0.0.12 and check",
        "deny:phone": "call me at 514-555-0199 later",
        "deny:path": "open ~/notes/plan.md and fix it",
        "deny:money": "I spent $42.10 on lunch, log it",
        "deny:private-name": "tell Zorblatt the plan is done",
    }

    def test_each_rule_class_is_caught(self):
        for rule, text in self.CASES.items():
            got = [m["rule"] for m in mine.scrub_check(text, ["Zorblatt"])]
            self.assertIn(rule, got, f"{rule}: {text!r} -> {got}")

    MORE = {
        "secret:stripe-key": "charge with " + "sk_" + "live_abcdefghijklmnop1234",
        "secret:google-key": "maps key " + "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
        "secret:hf-token": "pull with " + "hf_" + "abcdefghijklmnopqrstuvwx",
        "secret:password-is": "my password is tr0ub4dor, log in",
        "secret:split-key": "the key is " + "sk-ant- " + "api03abcdefghijklmnopqrstu",
        "deny:money": ["it was CAD 20 total", "USD 50 for hosting", "costs 20 CHF", "about 30 euros",
                       "only 99 cents", "paid ₹500 for it", "£12 at the shop"],
        "deny:hostname": "see internal-wiki.example/team/page for it",
        "deny:path": ["read ~bob/notes.txt first", r"copy \\fileserver\share\doc.txt", "/etc/hosts too"],
        "deny:phone": ["text +33 6 12 34 56 78 now", "ring 5145550199 later", "call +1 514 555 0199"],
    }

    def test_more_classes_are_caught(self):
        for rule, texts in self.MORE.items():
            for text in ([texts] if isinstance(texts, str) else texts):
                got = [m["rule"] for m in mine.scrub_check(text, [])]
                self.assertIn(rule, got, f"{rule}: {text!r} -> {got}")

    def test_private_names_normalised_on_both_sides(self):
        names = ["Zoë O’Brien-Kahl"]
        for t in ["ask zoe o'brien-kahl today", "ask ZOË  O’BRIEN–KAHL today", "ask Zoe\tO'Brien-Kahl"]:
            self.assertIn("deny:private-name", [m["rule"] for m in mine.scrub_check(t, names)], t)
        self.assertFalse(mine.scrub_check("ask zoe today", names))

    def test_ordinary_routing_prompts_pass(self):
        for t in ["explain recursion simply", "write a commit message for the parser fix", "summarise this PR",
                  "wrap up the session", "hand off to the next session", "eli5 the cache, 2 examples"]:
            self.assertEqual(mine.scrub_check(t, ["Zorblatt"]), [], t)

    def test_hints_show_rule_and_length_only(self):
        texts = list(self.CASES.values())
        for t in texts:
            for m in mine.scrub_check(t, ["Zorblatt"]):
                self.assertRegex(m["hint"], r"^\(\d+ chars\)$", (t, m))

    def test_comments_in_private_names_are_ignored(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "private-names.txt").write_text("# people\nZorblatt\n  \n#Quux\n", encoding="utf-8")
        self.assertEqual(mine.private_names(ws), ["Zorblatt"])

    def test_clean_case_passes(self):
        for t in ["explain the retry loop like I'm five", "/eli5 3",
                  "write a commit message for the staged diff", "and/or both, 3 times"]:
            self.assertEqual(mine.scrub_check(t, ["Zorblatt"]), [], t)

    def test_public_sets_pass(self):
        for p in sorted((ROOT / "sets").glob("*.json")):
            for c in json.loads(p.read_text())["cases"]:
                self.assertEqual(mine.scrub_check(c["prompt"], []), [], (p.name, c["id"]))

    def test_private_names_are_word_bounded_and_case_insensitive(self):
        self.assertTrue(mine.scrub_check("ask ZORBLATT", ["zorblatt"]))
        self.assertFalse(mine.scrub_check("ask zorblatter", ["zorblatt"]))

    def test_match_is_never_echoed_in_full(self):
        secret = "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz0123"  # built at runtime: fake
        for m in mine.scrub_check(f"key {secret}", []):
            self.assertNotIn(secret, json.dumps(m))
            self.assertNotIn("abcdefghij", json.dumps(m))

    def test_cli_reads_private_names_from_the_workspace(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "private-names.txt").write_text("# names\nZorblatt\n\n", encoding="utf-8")
        f = ws / "case.txt"
        f.write_text("tell zorblatt it shipped", encoding="utf-8")
        code, out = run(["--workspace", str(ws), "scrub-check", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("deny:private-name", out)
        self.assertNotIn("zorblatt", out.lower())
        f.write_text("tell the team it shipped", encoding="utf-8")
        self.assertEqual(run(["--workspace", str(ws), "scrub-check", str(f)])[0], 0)

    def test_cli_checks_every_case_of_a_set_file(self):
        ws = Path(tempfile.mkdtemp())
        f = ws / "set.json"
        f.write_text(json.dumps({"skill": "x", "cases": [
            {"id": "x-1", "prompt": "clean prompt", "expect": "trigger"},
            {"id": "x-2", "prompt": "mail a@b.co now", "expect": "no"}]}), encoding="utf-8")
        code, out = run(["scrub-check", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("x-2", out)
        self.assertNotIn("a@b.co", out)


class Host(unittest.TestCase):
    def test_loopback_hosts_allowed(self):
        for h in ["", "127.0.0.1", "127.0.0.1:11434", "http://127.0.0.1:11434", "localhost:11434",
                  "http://localhost", "[::1]:11434", "http://[::1]:11434"]:
            self.assertTrue(mine.check_host(h).startswith("http"), h)

    def test_non_loopback_refused(self):
        for h in ["0.0.0.0", "0.0.0.0:11434", "http://0.0.0.0:11434", "192.168.1.20:11434",
                  "https://ollama.example.org", "10.0.0.5", "[::]:11434", "127.0.0.1.example.org",
                  "http://localhost.evil.test:11434"]:
            with self.assertRaises(SystemExit, msg=h):
                mine.check_host(h)

    def test_scrub_cli_refuses_a_remote_host_before_any_call(self):
        ws = Path(tempfile.mkdtemp())
        hits = ws / "hits.jsonl"
        hits.write_text(json.dumps({"id": "h1", "skill": "eli5", "signal": "missed", "prompt": "p"}) + "\n")
        for h in ["0.0.0.0:11434", "http://ollama.example.org:11434"]:
            with mock.patch.dict(os.environ, {"OLLAMA_HOST": h}):
                with self.assertRaises(SystemExit) as e:
                    run(["scrub", "--hits", str(hits), "--workspace", str(ws)])
            self.assertIn("loopback", str(e.exception))
        self.assertFalse(list(ws.glob("mined/review-*")))


def fake_model(drafts: dict[str, str]):
    calls = []

    def call(prompt: str, skill: str, signal: str) -> str:
        calls.append(prompt)
        return drafts[prompt]
    call.calls = calls
    return call


HITS = [
    {"id": "aaaa1111", "signal": "corrected", "skill": "gen-commits",
     "prompt": "can you explain Zorblatt's retry loop in plain words"},
    {"id": "bbbb2222", "signal": "missed", "skill": "gen-commits",
     "prompt": "summarize the changes I made in ~/work/app as a commit message"},
]


class Scrub(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "private-names.txt").write_text("Zorblatt\n", encoding="utf-8")
        self.hits = self.ws / "mined" / "hits-2026-10-01.jsonl"
        self.hits.parent.mkdir(parents=True)
        self.hits.write_text("".join(json.dumps(h) + "\n" for h in HITS), encoding="utf-8")

    def scrub(self, drafts):
        model = fake_model(drafts)
        with contextlib.redirect_stdout(io.StringIO()):
            rows, path = mine.scrub(self.hits, self.ws, model)
        return rows, path, model

    def test_clean_draft_pending_leaky_draft_rejected(self):
        rows, path, model = self.scrub({
            HITS[0]["prompt"]: "can you explain the teammate's retry loop in plain words",
            HITS[1]["prompt"]: "summarize the changes I made in ~/work/app as a commit message"})
        self.assertEqual(len(model.calls), 2)
        a, b = rows
        self.assertEqual(a["status"], "pending")
        self.assertEqual(a["scrub_check"], {"ok": True, "rules": []})
        self.assertEqual(a["expect"], "no")          # the skill fired and was corrected
        self.assertEqual(b["status"], "rejected")
        self.assertIn("deny:path", b["scrub_check"]["rules"])
        self.assertEqual(b["expect"], "trigger")     # the user had to run it by hand
        for k in ("id", "skill", "signal", "raw_prompt", "scrubbed_prompt"):
            self.assertIn(k, a)
        md = path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(HITS[0]["prompt"], md)
        self.assertIn("teammate's retry loop", md)
        self.assertTrue(path.name.startswith("review-") and path.parent == self.ws / "mined")

    def test_second_scrub_the_same_day_keeps_the_first_review(self):
        drafts = {h["prompt"]: "explain the retry loop simply" for h in HITS}
        _, first, _ = self.scrub(drafts)
        saved = (first.read_bytes(), first.with_suffix(".md").read_bytes())
        _, second, _ = self.scrub({h["prompt"]: "a different draft" for h in HITS})
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_bytes(), first.with_suffix(".md").read_bytes()), saved)
        self.assertIn("a different draft", second.read_text())

    def test_assertion_error_in_the_model_call_is_not_swallowed(self):
        def boom(prompt, skill, signal):
            raise AssertionError("a test guard fired")
        with self.assertRaises(AssertionError):
            with contextlib.redirect_stdout(io.StringIO()):
                mine.scrub(self.hits, self.ws, boom)

    def test_model_error_is_a_rejected_row_not_a_crash(self):
        def broken(prompt, skill, signal):
            raise mine.ScrubError("ollama: connection refused")
        with contextlib.redirect_stdout(io.StringIO()):
            rows, _ = mine.scrub(self.hits, self.ws, broken)
        self.assertEqual({r["status"] for r in rows}, {"rejected"})
        self.assertIn("model:error", rows[0]["scrub_check"]["rules"])

    def test_cli_scrub_uses_a_loopback_scrubber(self):
        made = []

        def factory(model, host):
            made.append((model, host))
            return fake_model({h["prompt"]: "explain the retry loop simply" for h in HITS})
        with mock.patch.object(mine, "OllamaScrubber", factory), \
                mock.patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:11434"}):
            code, _ = run(["scrub", "--hits", str(self.hits), "--workspace", str(self.ws), "--model", "qwen3:8b"])
        self.assertEqual(code, 0)
        self.assertEqual(made, [("qwen3:8b", "http://127.0.0.1:11434")])
        self.assertTrue(list((self.ws / "mined").glob("review-*.jsonl")))


class Accept(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "private-names.txt").write_text("Zorblatt\n", encoding="utf-8")
        self.review = self.ws / "mined" / "review-2026-10-01.jsonl"
        self.review.parent.mkdir(parents=True)

    def write(self, rows):
        self.review.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def row(self, i, status, scrubbed="explain the retry loop simply", **kw):
        r = {"id": f"hit{i}", "skill": "eli5", "signal": "missed", "raw_prompt": "RAW Zorblatt text",
             "scrubbed_prompt": scrubbed, "expect": "trigger", "scrub_check": {"ok": True, "rules": []},
             "status": status}
        r.update(kw)
        return r

    def accept(self):
        return run(["accept", "--review", str(self.review), "--workspace", str(self.ws)])

    def test_accepted_and_edited_cases_join_a_workspace_copy(self):
        before = tree_digest(ROOT)
        public = json.loads((ROOT / "sets" / "eli5.json").read_text())
        self.write([self.row(1, "accepted"),
                    self.row(2, "edited", edited_prompt="put the cache idea in kid words"),
                    self.row(3, "dropped"), self.row(4, "pending")])
        code, out = self.accept()
        self.assertEqual(code, 0, out)
        s = json.loads((self.ws / "sets" / "eli5.json").read_text())
        mined_cases = [c for c in s["cases"] if c.get("source") == "mined"]
        self.assertEqual([c["prompt"] for c in mined_cases],
                         ["explain the retry loop simply", "put the cache idea in kid words"])
        self.assertTrue(all(c["expect"] == "trigger" for c in mined_cases))
        self.assertEqual(s["cases"][:len(public["cases"])], public["cases"])  # a copy of the public set
        self.assertNotIn("RAW", json.dumps(s))
        self.assertEqual(tree_digest(ROOT), before)  # sets/ never written
        # running accept again does not duplicate
        self.accept()
        s2 = json.loads((self.ws / "sets" / "eli5.json").read_text())
        self.assertEqual(len(s2["cases"]), len(s["cases"]))

    def test_leaky_edit_refuses_and_writes_nothing(self):
        self.write([self.row(1, "accepted"),
                    self.row(2, "edited", edited_prompt="ask Zorblatt to explain it simply")])
        with self.assertRaises(SystemExit) as e:
            self.accept()
        self.assertIn("deny:private-name", str(e.exception))
        self.assertNotIn("Zorblatt", str(e.exception))
        self.assertFalse((self.ws / "sets").exists())

    def test_pending_row_with_a_leaky_edit_refuses(self):
        self.write([self.row(1, "accepted"),
                    self.row(2, "pending", edited_prompt="mail it to x@y.org")])
        with self.assertRaises(SystemExit):
            self.accept()
        self.assertFalse((self.ws / "sets").exists())

    def test_accepting_a_rejected_draft_refuses(self):
        self.write([self.row(1, "accepted", scrubbed="open ~/private/notes.md")])
        with self.assertRaises(SystemExit):
            self.accept()

    def test_bad_status_or_expect_refuses(self):
        self.write([self.row(1, "maybe")])
        with self.assertRaises(SystemExit):
            self.accept()
        self.write([self.row(1, "accepted", expect="sometimes")])
        with self.assertRaises(SystemExit):
            self.accept()

    def test_a_row_missing_a_required_key_writes_nothing(self):
        for key in ("id", "skill", "signal", "expect", "status"):
            bad = self.row(2, "dropped")
            del bad[key]
            self.write([self.row(1, "accepted"), bad])
            with self.assertRaises(SystemExit) as e:
                self.accept()
            self.assertIn(key, str(e.exception))
            self.assertFalse((self.ws / "sets").exists(), key)

    def test_skill_without_a_public_set_gets_a_new_one(self):
        self.write([self.row(1, "accepted", skill="private-skill")])
        self.assertEqual(self.accept()[0], 0)
        s = json.loads((self.ws / "sets" / "private-skill.json").read_text())
        self.assertEqual(s["skill"], "private-skill")
        self.assertEqual(s["accept"], ["private-skill"])
        self.assertEqual(len(s["cases"]), 1)

    def test_skill_name_cannot_escape_the_sets_dir(self):
        self.write([self.row(1, "accepted", skill="../../evil")])
        with self.assertRaises(SystemExit):
            self.accept()

    def test_accept_refuses_a_workspace_inside_the_repo(self):
        self.write([self.row(1, "accepted")])
        with self.assertRaises(SystemExit):
            run(["accept", "--review", str(self.review), "--workspace", str(ROOT)])


class FakeOpener:
    def __init__(self, content=None, exc=None):
        self.content, self.exc, self.reqs = content, exc, []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        if self.exc:
            raise self.exc
        return io.BytesIO(json.dumps({"message": {"content": self.content}}).encode())


class Scrubber(unittest.TestCase):
    PROXIES = {"http_proxy": "http://proxy.example.test:3128", "HTTP_PROXY": "http://proxy.example.test:3128",
               "https_proxy": "http://proxy.example.test:3128", "all_proxy": "socks5://proxy.example.test:1080",
               "ALL_PROXY": "socks5://proxy.example.test:1080", "no_proxy": "", "NO_PROXY": ""}

    def test_proxy_env_is_never_used(self):
        with mock.patch.dict(os.environ, self.PROXIES):
            self.assertTrue(urllib.request.getproxies())  # the env really is set
            s = mine.OllamaScrubber("qwen3:8b", "127.0.0.1:11434")
        proxied = [h for h in s.opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
        self.assertEqual(proxied, [])

    def test_init_rechecks_the_host(self):
        for h in ("0.0.0.0:11434", "http://ollama.example.org"):
            with self.assertRaises(SystemExit):
                mine.OllamaScrubber("m", h, opener=FakeOpener('{"prompt": "x"}'))

    def test_request_shape_and_reply(self):
        op = FakeOpener('{"prompt": "  explain the loop simply  "}')
        s = mine.OllamaScrubber("qwen3:8b", "localhost:11434", opener=op)
        self.assertEqual(s("RAW prompt text", "eli5", "missed"), "explain the loop simply")
        req = op.reqs[0]
        self.assertEqual(req.full_url, "http://localhost:11434/api/chat")
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["stream"], body["format"]), ("qwen3:8b", False, "json"))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], mine.SCRUB_SYSTEM)
        self.assertIn("RAW prompt text", body["messages"][1]["content"])

    def test_bad_replies_are_scrub_errors(self):
        for op in (FakeOpener('{"prompt": "   "}'), FakeOpener("not json"), FakeOpener('{"other": 1}'),
                   FakeOpener('{"prompt": 3}'), FakeOpener(exc=OSError("connection refused"))):
            with self.assertRaises(mine.ScrubError):
                mine.OllamaScrubber("m", "127.0.0.1", opener=op)("p", "s", "missed")

    def test_assertion_error_passes_through(self):
        op = FakeOpener(exc=AssertionError("guard"))
        with self.assertRaises(AssertionError):
            mine.OllamaScrubber("m", "127.0.0.1", opener=op)("p", "s", "missed")


class WorkspaceRule(unittest.TestCase):
    def test_case_variant_of_the_repo_is_inside(self):
        alt = Path(str(ROOT).swapcase())
        if not alt.exists():
            self.skipTest("case-sensitive file system")
        self.assertTrue(mine.is_inside(alt, ROOT))
        self.assertTrue(mine.is_inside(alt / "private" / "ws", ROOT))
        with self.assertRaises(SystemExit):
            run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX), "--workspace", str(alt / "private")])

    def test_symlink_into_the_repo_is_inside(self):
        t = Path(tempfile.mkdtemp())
        (t / "link").symlink_to(ROOT)
        self.assertTrue(mine.is_inside(t / "link" / "ws", ROOT))
        self.assertTrue(mine.is_inside(t / "link" / "results" / "ws", ROOT))

    def test_dotdot_is_resolved(self):
        t = Path(tempfile.mkdtemp())
        root = t / "root"
        (root / "sub").mkdir(parents=True)
        self.assertFalse(mine.is_inside(root / ".." / "other", root))
        self.assertFalse(mine.is_inside(root / "sub" / ".." / ".." / "other", root))
        self.assertTrue(mine.is_inside(t / "other" / ".." / "root" / "x", root))

    def test_repo_is_refused_even_where_git_ignores_it(self):
        self.assertIn("inside the repo", mine.workspace_problem(ROOT / "results" / "private", ROOT))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitWorkTree(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.repo / "ignored").mkdir()

    def test_tracked_path_in_another_repo_is_refused(self):
        msg = mine.workspace_problem(self.repo / "private", ROOT)
        self.assertIsNotNone(msg)
        self.assertIn("git work tree", msg)
        with self.assertRaises(SystemExit):
            run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX),
                 "--workspace", str(self.repo / "private")])

    def test_ignored_path_in_another_repo_is_allowed(self):
        self.assertIsNone(mine.workspace_problem(self.repo / "ignored" / "skill-evals", ROOT))
        code, _ = run(["--skills-dir", EXAMPLES, "mine", "--transcripts", str(FIX),
                       "--workspace", str(self.repo / "ignored" / "skill-evals")])
        self.assertEqual(code, 0)

    def test_outside_any_repo_is_allowed(self):
        self.assertIsNone(mine.workspace_problem(Path(tempfile.mkdtemp()) / "ws", ROOT))

    def test_without_git_only_the_repo_is_refused(self):
        with mock.patch.object(mine.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(mine.workspace_problem(self.repo / "private", ROOT))
            self.assertIsNotNone(mine.workspace_problem(ROOT / "x", ROOT))


if __name__ == "__main__":
    unittest.main()
