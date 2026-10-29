"""skill-evals CLI.  Run `python3 -m skillevals -h`.

  lint            deterministic checks (+ self-tests declared in the sets)
  trigger         trigger evals through a judge -> results/<date>/<run>.jsonl + scorecards
  compare A B     which skills lost F1 and which prompts flipped (exit 1 on regression)
  history         rebuild HISTORY.md from every results file
  prove-can-fail  break one description IN MEMORY, show the score drops, restore, show it recovers
  report          rebuild REPORT.md (latest baselines, prove-can-fail verdicts, invalid runs)
  canary          behaviour canaries: does an agent under a skill refuse injected asks, keep working?
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from . import catalogue as cat_mod
from . import api, behaviour, judges, lint, prices, report, trigger

ROOT = Path(__file__).resolve().parent.parent
BROKEN_DESC = "Use when the user wants help pricing vintage furniture."
BREAK_MODES = {
    "wrong": lambda d: BROKEN_DESC,                                            # copy-paste accident
    "no-negatives": lambda d: re.split(r"\s*Do NOT use", d, flags=re.I)[0],    # guard-rails removed
}


def _out(args) -> Path:
    """Where results, scorecards, HISTORY.md and REPORT.md go: --workspace, else the repo."""
    return Path(args.workspace).expanduser() if getattr(args, "workspace", None) else ROOT


def _preflight(plan: list[tuple[str, int, int, int]], max_usd: float) -> None:
    """Real-dollar worst case for the anthropic: parts of a run; CLI and local backends add nothing."""
    if plan:
        worst = prices.preflight(plan, max_usd)
        print(f"preflight: worst case ${worst:.4f} <= --max-usd ${max_usd} (API prices as of {prices.PRICES_AS_OF})")


def _trigger_plan(args, sets, cat, judge, runs: int) -> list[tuple[str, int, int, int]]:
    if not isinstance(judge, judges.AnthropicJudge):
        return []
    # names padded so the bound also covers blind ids
    system = judges.ROUTER_SYSTEM.format(catalogue="\n".join(
        f"- skill-000-{s.name}: {s.description}" for s in cat.values()))
    longest = max((c["prompt"] for s in sets for c in s["cases"]), key=lambda t: len(t.encode("utf-8")))
    calls = sum(len(s["cases"]) for s in sets) * args.samples * runs
    return [(judge.model, calls, prices.estimate_tokens(system) + prices.estimate_tokens(longest)
             + prices.estimate_tokens(json.dumps(judges.PICK_SCHEMA)), judge.max_tokens)]


def _agent(spec: str):
    kind, _, model = spec.partition(":")
    if kind == "mutant":
        return behaviour.MutantAgent(model)
    if kind == "claude":
        return behaviour.ClaudeAgent(model or "sonnet")
    if kind == "anthropic":
        return behaviour.AnthropicAgent(*api.parse_spec(model or "claude-sonnet-5-5"))
    raise SystemExit(f"unknown agent {spec!r} (use claude[:model], anthropic:<model-id>[@effort], "
                     "mutant:always-refuse, mutant:always-comply)")


def _grader(spec: str):
    kind, _, model = spec.partition(":")
    if kind == "scripted":
        return behaviour.ScriptedGrader()
    if kind == "claude":
        return behaviour.ClaudeGrader(model or "haiku")
    if kind == "anthropic":
        return behaviour.AnthropicGrader(*api.parse_spec(model or "claude-opus-5-5"))
    raise SystemExit(f"unknown grader {spec!r} (use claude[:model], anthropic:<model-id>[@effort], scripted)")


def _canary_plan(cases, skill_text, agent, grader, reps: int, replies: list[str] | None = None):
    """Agent calls (none on a regrade) and one grader call per case, each at its worst case."""
    plan = []
    if not cases:
        return plan
    if isinstance(agent, behaviour.AnthropicAgent) and replies is None:
        system = behaviour.AGENT_FRAME.format(skill=skill_text)
        longest = max(len(behaviour.AGENT_USER.format(context=c["context"], task=c["task"]).encode("utf-8"))
                      for c in cases)
        plan.append((agent.model, len(cases) * reps, prices.estimate_tokens(system + "x" * longest),
                     agent.max_tokens))
    if isinstance(grader, behaviour.AnthropicGrader):
        longest = max(len(grader.prompt(c, []).encode("utf-8")) for c in cases)
        if replies is not None:
            reply_tokens = prices.estimate_tokens("".join(replies))
        else:  # not written yet: every reply is cut to REPLY_CHARS before grading, whatever the agent
            reply_tokens = reps * prices.estimate_tokens("x" * behaviour.REPLY_CHARS * 2)  # x2: non-ASCII bytes
        plan.append((grader.model, len(cases), prices.estimate_tokens(behaviour.GRADER_SYSTEM + "x" * longest)
                     + prices.estimate_tokens(json.dumps(behaviour.GRADES_SCHEMA)) + reply_tokens,
                     grader.max_tokens))
    return plan


def _sets(args) -> list[dict]:
    paths = sorted((ROOT / "sets").glob("*.json")) if not args.sets else [Path(p) for p in args.sets]
    sets = [trigger.load_set(p) for p in paths]
    if getattr(args, "skill", None):
        sets = [s for s in sets if s["skill"] in args.skill]
    if not sets:
        sys.exit("no eval sets matched")
    return sets


def _checks(sets, skills_dir: Path, network: bool, run_selftests: bool = False) -> list[dict]:
    out = []
    for s in sets:
        if s.get("draft"):  # not installed yet: nothing under skills_dir to lint
            out.append(lint.Check(s["skill"], "installed", "SKIP", f"draft, not installed: {s['draft']}"))
            continue
        out += lint.lint_skill(skills_dir / s["skill"], s.get("path_ignore"))
        out += lint.run_selftests(s["skill"], s.get("selftests", []), network, run_selftests)
    return [asdict(c) for c in out]


def _skills_dir(args) -> Path:
    d = Path(args.skills_dir).expanduser()
    if not d.is_dir():
        raise SystemExit(f"skills dir not found: {d} (pass --skills-dir, before the subcommand)")
    return d


def cmd_lint(args) -> int:
    sets = _sets(args)
    checks = _checks(sets, _skills_dir(args), args.network, args.run_selftests)
    for c in checks:
        print(f"{c['level']:<5} {c['skill']:<12} {c['check']:<28} {c['detail']}")
    fails = sum(c["level"] == "FAIL" for c in checks)
    print(f"\nlint: {len(checks)} checks, {fails} FAIL, {sum(c['level'] == 'WARN' for c in checks)} WARN")
    return 1 if fails else 0


def _overrides(args, cat) -> dict[str, str]:
    ov = {}
    for spec in args.override or []:
        name, _, f = spec.partition("=")
        ov[name] = Path(f).read_text(encoding="utf-8").strip()
    for name in args.break_skill or []:
        ov[name] = BREAK_MODES[args.break_mode](cat[name].description)
    return ov


def _trigger(args, sets, judge, cat, variant) -> list[dict]:
    if args.blind_names:
        variant += "-blind"
    run_id = trigger.new_run_id(judge.label, variant)
    print(f"run {run_id}  judge={judge.label}  cases={sum(len(s['cases']) for s in sets) * args.samples}")
    rows = trigger.run(sets, judge, cat, run_id, variant, args.max_usd, args.samples, blind=args.blind_names)
    out = _out(args)
    p = trigger.write_jsonl(rows, out)
    checks = [] if args.no_lint else _checks(sets, Path(args.skills_dir).expanduser(), args.network,
                                             args.run_selftests)
    sc = report.write_scorecards(rows, checks, out)
    if not getattr(args, "no_report", False):
        report.history(out)
        report.write_summary(out)
    print(f"results:    {p.relative_to(out)}\nscorecards: {sc.relative_to(out)}/_summary.md")
    print((sc / "_summary.md").read_text())
    return rows


def cmd_trigger(args) -> int:
    sets = _sets(args)
    cat = cat_mod.with_drafts(cat_mod.load(Path(args.skills_dir).expanduser()), sets)
    ov = _overrides(args, cat)
    variant = args.variant or ("override" if ov else "baseline")
    judge = judges.make(args.judge)
    cat = cat_mod.with_overrides(cat, ov)
    _preflight(_trigger_plan(args, sets, cat, judge, runs=1), args.max_usd)
    rows = _trigger(args, sets, judge, cat, variant)
    return 1 if any(r.get("error") == "budget cap reached" for r in rows) else 0


def cmd_compare(args) -> int:
    a, b = report.trigger_rows(Path(args.a)), report.trigger_rows(Path(args.b))
    for p, rows in ((args.a, a), (args.b, b)):
        if not rows:
            raise SystemExit(f"no trigger rows in results file: {p} (canary runs are not comparable here)")
    text = report.compare(a, b)
    print(text)
    return 1 if "REGRESSIONS: 0 skill(s)" not in text else 0


def cmd_history(args) -> int:
    print(report.history(_out(args)))
    return 0


def cmd_report(args) -> int:
    p = report.write_summary(_out(args))
    print(p.read_text())
    return 0


def _file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def cmd_prove(args) -> int:
    """Test the test: if a broken description does NOT drop the score, the harness is blind."""
    args.skill = [args.target]
    sets = _sets(args)
    skills_dir = Path(args.skills_dir).expanduser()
    cat = cat_mod.with_drafts(cat_mod.load(skills_dir), sets)  # a set's draft joins in memory
    if args.target not in cat:
        raise SystemExit(f"{args.target!r} is neither installed in {skills_dir} nor a set's draft")
    real = Path(cat[args.target].path)  # the installed SKILL.md, or the draft file
    before_sha = _file_sha(real)
    judge = judges.make(args.judge)
    _preflight(_trigger_plan(args, sets, cat, judge, runs=3), args.max_usd)  # baseline, broken, restored
    args.no_lint = True
    f1, errs = {}, {}
    for variant, ov in [("baseline", {}),
                        (f"broken-{args.break_mode}", {args.target: BREAK_MODES[args.break_mode](cat[args.target].description)}),
                        ("restored", {})]:
        print(f"\n=== {variant}")
        rows = _trigger(args, sets, judge, cat_mod.with_overrides(cat, ov), variant)
        f1[variant] = trigger.Metrics.of(rows).f1
        errs[variant] = trigger.Metrics.of(rows).errors
    after_sha = _file_sha(real)
    broken = f"broken-{args.break_mode}"
    drop = (f1["baseline"] or 0) - (f1[broken] or 0)
    recovered = (f1["restored"] or 0) >= (f1["baseline"] or 0) - args.tolerance
    errored = [v for v, n in errs.items() if n]
    ok = drop >= args.min_drop and recovered and before_sha == after_sha and not errored
    print(f"\nPROVE-CAN-FAIL {args.target}: F1 baseline={trigger.fmt(f1['baseline'])} "
          f"broken={trigger.fmt(f1[broken])} restored={trigger.fmt(f1['restored'])} drop={drop:+.2f} "
          f"(need >= {args.min_drop}) · SKILL.md sha {before_sha} -> {after_sha} "
          f"({'unchanged' if before_sha == after_sha else 'CHANGED!'})")
    if errored:
        print(f"RESULT: INCONCLUSIVE - judge errors in {', '.join(errored)}; rerun, do not read the scores")
        return 2
    print("RESULT:", "PASS - the harness can fail" if ok else "FAIL - the harness did not detect the break")
    return 0 if ok else 1


def cmd_canary(args) -> int:
    cset = behaviour.load(Path(args.canaries))
    draft = Path(args.skill_file).expanduser() if args.skill_file else cat_mod.resolve_draft(cset["draft"])
    skill_text = behaviour.skill_body(draft)
    agent, grader = _agent(args.agent), _grader(args.grader)
    run_id = behaviour.new_run_id(agent.label)
    before = _file_sha(draft)
    if args.regrade:
        old = report.load_rows(Path(args.regrade))
        if args.case:
            old = [r for r in old if r["case_id"] in args.case]
        n = len({r["case_id"] for r in old})
        if n > args.max_calls:
            raise SystemExit(f"regrade needs {n} grader calls > cap {args.max_calls}")
        ids = {r["case_id"] for r in old}
        _preflight(_canary_plan([c for c in cset["canaries"] if c["id"] in ids], skill_text, agent, grader,
                                args.reps, replies=[r.get("reply") or "" for r in old]), args.max_usd)
        run_id = run_id.replace("-canary-", "-canary-regrade-")
        print(f"regrade {args.regrade} -> {run_id}  grader={grader.label}  ({n} grader calls, 0 agent calls)")
        rows = behaviour.regrade(old, cset, grader, run_id)
        for r in rows:
            r["date"] = run_id[:10]
        return _canary_out(rows, draft, before, _out(args))
    cases = [c for c in cset["canaries"] if not args.case or c["id"] in args.case]
    _preflight(_canary_plan(cases, skill_text, agent, grader, args.reps), args.max_usd)
    print(f"run {run_id}  agent={agent.label}  grader={grader.label}  reps={args.reps}  skill sha {before}")
    rows = behaviour.run(cset, skill_text, agent, grader, run_id, args.reps, args.max_calls, args.case,
                         max_usd=args.max_usd)
    for r in rows:
        r["skill_sha"] = before
    return _canary_out(rows, draft, before, _out(args))


def _canary_out(rows, draft, before, out: Path) -> int:
    run_id = rows[0]["run_id"]
    p = behaviour.write_jsonl(rows, out)
    v = behaviour.verdict(rows)
    md = behaviour.render(rows, v)
    sc = out / "scorecards" / rows[0]["date"] / f"{run_id}.md"
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(md, encoding="utf-8")
    after = _file_sha(draft)
    print(f"results:   {p.relative_to(out)}\nscorecard: {sc.relative_to(out)}")
    print(f"skill sha {before} -> {after} ({'unchanged' if before == after else 'CHANGED!'})")
    print(f"RESULT: {v['result']}  failed={v['failed_cases']}  refuse_ok={v['refuse_ok']}  "
          f"comply_ok={v['comply_ok']}  errors={v['errors']}  {report.fmt_costs(v['costs'])}")
    return {"PASS": 0, "FAIL": 1}.get(v["result"], 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skillevals", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills-dir", default=str(cat_mod.DEFAULT_SKILLS_DIR))
    ap.add_argument("--workspace", help="write results, scorecards, HISTORY.md and REPORT.md here "
                                        "instead of the repo (use for private skills and cases)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--sets", nargs="*", help="eval set files (default: sets/*.json)")
        p.add_argument("--network", action="store_true", help="also run self-tests that need network")
        p.add_argument("--run-selftests", action="store_true",
                       help="run the sets' self-test commands (bash -c, your rights); off by default")

    p = sub.add_parser("lint"); common(p); p.add_argument("--skill", nargs="*")
    p.set_defaults(fn=cmd_lint)

    def judged(p):
        common(p)
        p.add_argument("--judge", default="ollama:qwen3:8b",
                       help="claude[:model] | anthropic:<model-id>[@effort] | ollama[:model] | scripted")
        p.add_argument("--max-usd", type=float, default=1.0,
                       help="hard cap on judge spend for the run (real dollars for anthropic:)")
        p.add_argument("--samples", type=int, default=1, help="repeat each prompt N times (flakiness)")
        p.add_argument("--no-lint", action="store_true")
        p.add_argument("--break-mode", choices=sorted(BREAK_MODES), default="wrong")
        p.add_argument("--blind-names", action="store_true",
                       help="hide skill names behind opaque ids so only descriptions can route")

    p = sub.add_parser("trigger"); judged(p)
    p.add_argument("--skill", nargs="*")
    p.add_argument("--override", nargs="*", help="skill=FILE: use FILE's text as the description (in memory)")
    p.add_argument("--break-skill", nargs="*", help="replace these skills' descriptions with a broken one")
    p.add_argument("--variant")
    p.add_argument("--no-report", action="store_true", help="do not rebuild HISTORY.md / REPORT.md")
    p.set_defaults(fn=cmd_trigger)

    p = sub.add_parser("canary", help="behaviour canaries (refuse/comply) for one skill")
    p.add_argument("--canaries", default=str(ROOT / "canaries" / "overnight-runner.json"))
    p.add_argument("--skill-file", help="SKILL.md to load as the agent's system prompt (default: the set's draft)")
    p.add_argument("--agent", default="claude:sonnet", help="claude[:model] | anthropic:<model-id>[@effort] | mutant:always-refuse | mutant:always-comply")
    p.add_argument("--grader", default="claude:haiku", help="claude[:model] | anthropic:<model-id>[@effort] | scripted")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--max-calls", type=int, default=60, help="refuse to start if the plan needs more claude calls")
    p.add_argument("--max-usd", type=float, default=0.5,
                   help="hard stop: no new call once the run has spent this much (CLI list price; real dollars "
                        "for anthropic:, which also refuses to start if the worst case is over it)")
    p.add_argument("--case", nargs="*", help="only these canary ids")
    p.add_argument("--regrade", help="results .jsonl: grade its saved replies again (no agent calls)")
    p.set_defaults(fn=cmd_canary)

    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("history"); p.set_defaults(fn=cmd_history)
    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report)

    p = sub.add_parser("prove-can-fail"); judged(p)
    p.add_argument("target")
    p.add_argument("--min-drop", type=float, default=0.3)
    p.add_argument("--tolerance", type=float, default=0.15, help="restored may sit this far under baseline (judge noise)")
    p.set_defaults(fn=cmd_prove)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
