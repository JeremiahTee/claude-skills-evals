"""skill-evals CLI.  Run `python3 -m skillevals -h`.

  lint            deterministic checks (+ self-tests declared in the sets)
  trigger         trigger evals through a judge -> results/<date>/<run>.jsonl + scorecards
  compare A B     which skills lost F1 and which prompts flipped (exit 1 on regression)
  history         rebuild HISTORY.md from every results file
  prove-can-fail  break one description IN MEMORY, show the score drops, restore, show it recovers
  report          rebuild REPORT.md (latest baselines, prove-can-fail verdicts, invalid runs)
  canary          behaviour canaries: does an agent under a skill refuse injected asks, keep working?
  mine            flag routing misses in your Claude Code transcripts (local; needs --workspace)
  scrub-check     deterministic secret + deny-list check of case text
  scrub           a loopback Ollama model rewrites mined hits into cases for review
  accept          reviewed cases join a copy of the skill's set under the workspace
  models          plan (and with --go run) the anthropic: model comparison; --report rebuilds its table
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import catalogue as cat_mod
from . import api, behaviour, judges, lint, mine, models, prices, report, trigger

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


# One saved reply as a grader reads it: up to REPLY_CHARS characters of up to 4 UTF-8 bytes
# each, plus its "[n]\n" header and the blank line between replies.
REPLY_BOUND_TOKENS = prices.estimate_tokens("x" * (behaviour.REPLY_CHARS * 4 + 16))


def _canary_plan(cases, skill_text, agent, grader, reps: int, replies: dict[str, list[str]] | None = None):
    """Agent calls (none on a regrade) and one grader call per case, each at its worst case.
    replies (a regrade): case id -> that case's saved replies; each grader call reads one case's."""
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
        schema = prices.estimate_tokens(json.dumps(behaviour.GRADES_SCHEMA))
        if replies is not None:  # saved replies: price each case's call on its own replies
            for c in cases:
                plan.append((grader.model, 1, prices.estimate_tokens(
                    behaviour.GRADER_SYSTEM + grader.prompt(c, replies.get(c["id"], []))) + schema, grader.max_tokens))
            return plan
        longest = max(len(grader.prompt(c, []).encode("utf-8")) for c in cases)
        # not written yet: every reply is cut to REPLY_CHARS before grading, whatever the agent
        reply_tokens = reps * REPLY_BOUND_TOKENS
        plan.append((grader.model, len(cases), prices.estimate_tokens(behaviour.GRADER_SYSTEM + "x" * longest)
                     + schema + reply_tokens, grader.max_tokens))
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


def _written(args, p: Path) -> None:
    """Tell a caller (models --go) that a results file exists, before the scorecard and report
    work that could still crash: a paid run's file is then never missing from its manifest."""
    hook = getattr(args, "on_results", None)
    if hook:
        hook(p)


def _trigger(args, sets, judge, cat, variant) -> tuple[list[dict], Path]:
    if args.blind_names:
        variant += "-blind"
    run_id = trigger.new_run_id(judge.label, variant)
    print(f"run {run_id}  judge={judge.label}  cases={sum(len(s['cases']) for s in sets) * args.samples}")
    rows = trigger.run(sets, judge, cat, run_id, variant, args.max_usd, args.samples, blind=args.blind_names)
    out = _out(args)
    p = trigger.write_jsonl(rows, out)
    _written(args, p)
    checks = [] if args.no_lint else _checks(sets, Path(args.skills_dir).expanduser(), args.network,
                                             args.run_selftests)
    sc = report.write_scorecards(rows, checks, out)
    if not getattr(args, "no_report", False):
        report.history(out)
        report.write_summary(out)
    print(f"results:    {p.relative_to(out)}\nscorecards: {sc.relative_to(out)}/_summary.md")
    print((sc / "_summary.md").read_text())
    return rows, p


def _run_trigger(args) -> tuple[list[dict], Path]:
    sets = _sets(args)
    cat = cat_mod.with_drafts(cat_mod.load(Path(args.skills_dir).expanduser()), sets)
    ov = _overrides(args, cat)
    variant = args.variant or ("override" if ov else "baseline")
    judge = judges.make(args.judge)
    cat = cat_mod.with_overrides(cat, ov)
    _preflight(_trigger_plan(args, sets, cat, judge, runs=1), args.max_usd)
    return _trigger(args, sets, judge, cat, variant)


def cmd_trigger(args) -> int:
    rows, _ = _run_trigger(args)
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
        rows, _ = _trigger(args, sets, judge, cat_mod.with_overrides(cat, ov), variant)
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


def _canary_inputs(args) -> tuple[dict, Path, str]:
    cset = behaviour.load(Path(args.canaries))
    draft = Path(args.skill_file).expanduser() if args.skill_file else cat_mod.resolve_draft(cset["draft"])
    return cset, draft, behaviour.skill_body(draft)


def cmd_canary(args) -> int:
    return _canary(args)[0]


def _canary(args) -> tuple[int, list[dict], Path]:
    """One canary run or regrade: (exit code, rows, results file)."""
    cset, draft, skill_text = _canary_inputs(args)
    agent, grader = _agent(args.agent), _grader(args.grader)
    run_id = behaviour.new_run_id(agent.label)
    before = _file_sha(draft)
    if args.regrade:
        old = report.load_rows(Path(args.regrade))
        if args.case:
            old = [r for r in old if r["case_id"] in args.case]
        agents = {r.get("agent") for r in old}
        if len(agents) != 1:
            raise SystemExit(f"regrade needs one agent per results file, found {len(agents)}: {sorted(map(str, agents))}")
        ids = {r["case_id"] for r in old if not r["error"]}  # a case with no graded reply gets no grader call
        n = len(ids)
        if not n:
            raise SystemExit(f"regrade: no saved reply to grade in {args.regrade} (every row errored)")
        if n > args.max_calls:
            raise SystemExit(f"regrade needs {n} grader calls > cap {args.max_calls}")
        saved: dict[str, list[str]] = {}
        for r in old:
            if not r["error"]:
                saved.setdefault(r["case_id"], []).append(r.get("reply") or "")
        _preflight(_canary_plan([c for c in cset["canaries"] if c["id"] in ids], skill_text, agent, grader,
                                args.reps, replies=saved), args.max_usd)
        # named after the agent whose replies these are, not --agent (unused by a regrade)
        run_id = behaviour.new_run_id(agents.pop()).replace("-canary-", "-canary-regrade-")
        print(f"regrade {args.regrade} -> {run_id}  grader={grader.label}  ({n} grader calls, 0 agent calls)")
        rows = behaviour.regrade(old, cset, grader, run_id)
        for r in rows:
            r["date"] = run_id[:10]
        return _canary_out(rows, draft, before, _out(args), args)
    cases = [c for c in cset["canaries"] if not args.case or c["id"] in args.case]
    _preflight(_canary_plan(cases, skill_text, agent, grader, args.reps), args.max_usd)
    print(f"run {run_id}  agent={agent.label}  grader={grader.label}  reps={args.reps}  skill sha {before}")
    rows = behaviour.run(cset, skill_text, agent, grader, run_id, args.reps, args.max_calls, args.case,
                         max_usd=args.max_usd)
    for r in rows:
        r["skill_sha"] = before
    return _canary_out(rows, draft, before, _out(args), args)


def _canary_out(rows, draft, before, out: Path, args=None) -> tuple[int, list[dict], Path]:
    p = behaviour.write_jsonl(rows, out)
    run_id = rows[0]["run_id"]  # after the write: an existing file of that name gives it a suffix
    _written(args, p)
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
    return {"PASS": 0, "FAIL": 1}.get(v["result"], 2), rows, p


def _date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}")


def _private_ws(args) -> Path:
    """mine/scrub/accept hold raw transcript text: a workspace is required, and never the repo."""
    ws = getattr(args, "workspace", None)
    if not ws:
        raise SystemExit(f"{args.cmd}: --workspace DIR is required (raw transcript text never goes in the repo)")
    ws = Path(ws).expanduser()
    why = mine.workspace_problem(ws, ROOT)
    if why:
        raise SystemExit(f"{args.cmd}: refusing workspace {ws}: {why}; use a private folder")
    return ws


def cmd_mine(args) -> int:
    ws = _private_ws(args)
    src = Path(args.transcripts).expanduser()
    if not src.is_dir():
        raise SystemExit(f"transcripts folder not found: {src}")
    known = set(cat_mod.load(_skills_dir(args)))
    hits, stats = mine.mine(src, known, args.since)
    p = mine.write_jsonl(mine.free_path(ws / "mined", f"hits-{mine._today()}"), hits, exclusive=True)
    counts = {s: sum(h["signal"] == s for h in hits) for s in ("corrected", "missed", "interrupted")}
    print(f"mined {stats['files']} files, {len(known)} known skills -> {p}")
    print("hits: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + f" (total {len(hits)})")
    print("skipped: " + ", ".join(f"{k} {v}" for k, v in stats.items() if k != "files"))
    return 0


def cmd_scrub_check(args) -> int:
    ws = getattr(args, "workspace", None)
    names = mine.private_names(Path(ws).expanduser() if ws else None)
    bad = 0
    texts = mine.check_texts(Path(args.file))
    for label, text in texts:
        found = mine.scrub_check(text or "", names)
        if found:
            bad += 1
            print(f"FAIL {label}: " + ", ".join(f"{m['rule']} {m['hint']}" for m in found))
    print(f"scrub-check: {len(texts)} checked, {bad} failed"
          + ("" if names else " (no private-names.txt in a --workspace: names not checked)"))
    return 1 if bad else 0


def cmd_scrub(args) -> int:
    ws = _private_ws(args)
    host = mine.check_host(os.environ.get("OLLAMA_HOST"))  # before anything reads a hit
    model = mine.OllamaScrubber(args.model, host)
    rows, p = mine.scrub(Path(args.hits), ws, model)
    ok = sum(r["status"] == "pending" for r in rows)
    print(f"scrubbed {len(rows)} hits with ollama:{args.model} at {host}: {ok} pending review, "
          f"{len(rows) - ok} rejected\nreview: {p}\n        {p.with_suffix('.md')}")
    return 0


def cmd_accept(args) -> int:
    ws = _private_ws(args)
    added = mine.accept(Path(args.review), ws, ROOT / "sets")
    for skill, n in added.items():
        print(f"{skill}: {n} mined case(s) added -> {ws / 'sets' / (skill + '.json')}")
    if not added:
        print("accept: no accepted or edited rows")
    return 0


MODELS_HELP = """Plan the three-model comparison (docs/design-api-backend.md section 2): for each model,
--runs trigger runs with real names and --runs with blind names, one canary run with the model as
agent graded by --grader1, and one --regrade of that canary by --grader2. Prints every planned
run's worst case and the plan total, then exits: no model is called without --go."""


def _count(text: str) -> int:
    """argparse type for a run/rep/sample count: a whole number of at least 1."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _api_spec(spec: str, flag: str) -> str:
    if not spec.startswith("anthropic:"):
        raise SystemExit(f"models compares anthropic: specs only (priced in real dollars); {flag} got {spec!r}")
    return spec


def _models_plan(args) -> dict:
    """Every run of the comparison, in order, with its worst case from the run commands' own plans."""
    if not args.models or not args.grader1 or not args.grader2:
        raise SystemExit("models needs --models, --grader1 and --grader2 (or --report MANIFEST)")
    for spec in args.models:
        _api_spec(spec, "--models")
    g1, g2 = _grader(_api_spec(args.grader1, "--grader1")), _grader(_api_spec(args.grader2, "--grader2"))
    sets = _sets(args)
    cat = cat_mod.with_drafts(cat_mod.load(Path(args.skills_dir).expanduser()), sets)
    cset, _, skill_text = _canary_inputs(args)
    cases = cset["canaries"]
    one_sample = argparse.Namespace(samples=1)
    steps = []
    for spec in args.models:
        judge = judges.make(spec)
        for k in range(1, args.runs + 1):
            for names in ("real", "blind"):
                steps.append({"kind": "trigger", "model": spec, "run": k, "names": names,
                              "plan": _trigger_plan(one_sample, sets, cat, judge, runs=1)})
        steps.append({"kind": "canary", "model": spec, "grader": args.grader1,
                      "max_calls": behaviour.planned_calls(cases, _agent(spec), g1, args.reps),
                      "plan": _canary_plan(cases, skill_text, _agent(spec), g1, args.reps)})
        # before the replies exist: one grader call per case, each reading that case's reps replies
        steps.append({"kind": "regrade", "model": spec, "grader": args.grader2, "max_calls": len(cases),
                      "plan": _canary_plan(cases, skill_text, None, g2, args.reps)})
    for s in steps:
        s["worst_usd"] = sum(prices.worst_case(*p) for p in s["plan"])
        s["status"] = "planned"
    return {"steps": steps, "total": sum(s["worst_usd"] for s in steps)}


def _print_plan(plan: dict) -> None:
    print(f"{'step':<5} {'kind':<8} {'model':<38} {'run':<4} {'names / grader':<34} worst case")
    for n, s in enumerate(plan["steps"], 1):
        print(f"{n:<5} {s['kind']:<8} {s['model']:<38} {str(s.get('run', '-')):<4} "
              f"{s.get('names') or s.get('grader'):<34} ${s['worst_usd']:.4f}")
    print(f"plan total: ${plan['total']:.4f} (API prices as of {prices.PRICES_AS_OF}; every call at the "
          "cache-write input rate plus its full max_tokens of output)")


def _step_argv(args, step: dict, canary_results: Path | None) -> list[str]:
    """The run command a step is: the existing trigger / canary / canary --regrade, capped at the
    step's own worst case."""
    head = ["--skills-dir", args.skills_dir, "--workspace", args.workspace]
    cap = repr(step["worst_usd"])  # repr round-trips the float, so the run's own preflight matches
    if step["kind"] == "trigger":
        argv = head + ["trigger", "--judge", step["model"], "--max-usd", cap]
        argv += ["--sets", *args.sets] if args.sets else []
        return argv + (["--blind-names"] if step["names"] == "blind" else [])
    argv = head + ["canary", "--canaries", args.canaries, "--grader", step["grader"], "--reps", str(args.reps),
                   "--max-usd", cap, "--max-calls", str(step["max_calls"])]
    argv += ["--skill-file", args.skill_file] if args.skill_file else []
    if step["kind"] == "canary":
        return argv + ["--agent", step["model"]]
    return argv + ["--regrade", str(canary_results)]


def _run_step(args, step: dict) -> tuple[list[dict], Path]:
    if step["kind"] == "trigger":
        return _run_trigger(args)
    _, rows, p = _canary(args)
    return rows, p


def cmd_models(args) -> int:
    if args.report:
        p = models.write_report(Path(args.report), _out(args) if args.workspace else None)
        print(p.read_text(encoding="utf-8"))
        print(f"report: {p}")
        return 0
    plan = _models_plan(args)
    _print_plan(plan)
    if not args.go:
        print("plan only: no model was called. Add --go --max-usd <total> (and --workspace DIR) to run it.")
        return 0
    if not args.workspace:
        raise SystemExit("models --go needs --workspace DIR: comparison runs always write outside the repo")
    if args.max_usd is None:
        raise SystemExit("models --go needs --max-usd: the plan total must fit under it")
    empty = [n for n, s in enumerate(plan["steps"], 1) if not s["worst_usd"] > 0]
    if empty:  # every step makes paid calls; a zero or negative bound means the plan is wrong
        raise SystemExit(f"worst case not above $0 for step(s) {empty}: refusing to start (a bad count or price?)")
    if plan["total"] > args.max_usd:
        raise SystemExit(f"plan total ${plan['total']:.4f} > --max-usd ${args.max_usd}: refusing to start "
                         f"(prices as of {prices.PRICES_AS_OF}). Raise the cap or shrink the plan.")
    ws = _out(args)
    plan_id = f"{datetime.now():%Y-%m-%dT%H%M%S}"
    (ws / "results" / "models").mkdir(parents=True, exist_ok=True)
    k = 1
    while True:  # created exclusively: another plan started this second keeps its manifest
        mpath = ws / "results" / "models" / (f"{plan_id}.json" if k == 1 else f"{plan_id}-{k}.json")
        try:
            mpath.open("x", encoding="utf-8").close()
            break
        except FileExistsError:
            k += 1
    plan_id = mpath.stem
    manifest = {"plan_id": plan_id, "models": args.models, "grader1": args.grader1, "grader2": args.grader2,
                "runs": args.runs, "reps": args.reps, "canaries": Path(args.canaries).name,
                "prices_as_of": prices.PRICES_AS_OF, "max_usd": args.max_usd, "total_usd": plan["total"],
                "stopped": None, "steps": plan["steps"]}

    def save():
        mpath.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")

    def finish() -> Path:
        for s in plan["steps"]:
            if s["status"] in ("planned", "written"):
                s["status"] = "not run" if s["status"] == "planned" else "crashed-after-write"
        save()
        return models.write_report(mpath, ws)

    save()
    canary_results, canary_rows = None, []
    for n, step in enumerate(plan["steps"], 1):
        print(f"\n=== models step {n}/{len(plan['steps'])}: {step['kind']} {step['model']} "
              f"(cap ${step['worst_usd']:.4f})")
        if step["kind"] == "regrade" and not any(not r["error"] for r in canary_rows):
            # every canary row errored (an INCONCLUSIVE result, not a stop): nothing to grade, no call
            step["status"] = "skipped (no graded reply to regrade)"
            save()
            continue
        def written(p: Path, step=step) -> None:  # the paid results file exists: record it now
            rel = str(p.relative_to(ws))
            if any(s.get("results") == rel for s in plan["steps"] if s is not step):
                raise RuntimeError(f"results file collision: {rel} (an earlier run's file was overwritten)")
            step.update(results=rel, status="written")
            save()

        try:
            step_args = build_parser().parse_args(_step_argv(args, step, canary_results))
            step_args.on_results = written
            rows, p = _run_step(step_args, step)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a crashed step stops the plan
            # crashed after its results file was written: the report still counts what it spent
            status = "crashed-after-write" if step.get("results") else "crashed"
            step.update(status=status, error=f"{type(e).__name__}: {e}"[:300])
            manifest["stopped"] = f"step {n} ({step['kind']} {step['model']}) crashed: {step['error']}"
            if isinstance(e, AssertionError):  # also the tests' no-real-call guard: record, then re-raise
                finish()
                raise
            break
        except BaseException as e:  # Ctrl-C: record where the plan stopped, then stop
            status = "crashed-after-write" if step.get("results") else "interrupted"
            step.update(status=status, error=type(e).__name__)
            manifest["stopped"] = f"step {n} ({step['kind']} {step['model']}) interrupted"
            finish()
            raise
        finally:
            save()
        if step.get("results") != str(p.relative_to(ws)):  # the run wrote without telling us
            step.update(status="crashed", error=f"results file not recorded: {p.relative_to(ws)}")
            manifest["stopped"] = f"step {n} ({step['kind']} {step['model']}) {step['error']}"
            break
        if step["kind"] == "canary":
            canary_results, canary_rows = p, rows
        capped = sum("budget cap reached" in str(r.get("error") or "") for r in rows)
        step["status"] = "cap" if capped else "done"
        if capped:
            manifest["stopped"] = f"step {n} ({step['kind']} {step['model']}): budget cap reached on {capped} row(s)"
            break
        save()
    rp = finish()
    print(f"\nmanifest: {mpath.relative_to(ws)}\nreport:   {rp.relative_to(ws)}")
    if manifest["stopped"]:
        print(f"STOPPED: {manifest['stopped']}")
    return 1 if manifest["stopped"] else 0


def build_parser() -> argparse.ArgumentParser:
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
        p.add_argument("--samples", type=_count, default=1, help="repeat each prompt N times (flakiness)")
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
    p.add_argument("--reps", type=_count, default=3)
    p.add_argument("--max-calls", type=int, default=60, help="refuse to start if the plan needs more claude calls")
    p.add_argument("--max-usd", type=float, default=0.5,
                   help="hard stop: no new call once the run has spent this much (CLI list price; real dollars "
                        "for anthropic:, which also refuses to start if the worst case is over it)")
    p.add_argument("--case", nargs="*", help="only these canary ids")
    p.add_argument("--regrade", help="results .jsonl: grade its saved replies again (no agent calls; --agent is ignored)")
    p.set_defaults(fn=cmd_canary)

    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("history"); p.set_defaults(fn=cmd_history)
    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report)

    p = sub.add_parser("prove-can-fail"); judged(p)
    p.add_argument("target")
    p.add_argument("--min-drop", type=float, default=0.3)
    p.add_argument("--tolerance", type=float, default=0.15, help="restored may sit this far under baseline (judge noise)")
    p.set_defaults(fn=cmd_prove)

    # Red cases from your own transcripts: local only, everything under a private workspace.
    def private_ws(p):  # --workspace also accepted after the subcommand; SUPPRESS keeps a value given before it
        p.add_argument("--workspace", default=argparse.SUPPRESS,
                       help="private workspace (required, outside the repo)")

    p = sub.add_parser("mine", help="flag routing misses in Claude Code transcripts -> WS/mined/hits-<date>.jsonl")
    p.add_argument("--transcripts", required=True, help="folder of Claude Code *.jsonl transcripts (searched recursively)")
    p.add_argument("--since", type=_date, help="only hits whose prompt is on or after YYYY-MM-DD")
    private_ws(p); p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("scrub-check", help="deterministic secret + deny-list check of case text (exit 1 on a match)")
    p.add_argument("file", help="a set .json, a hits/review .jsonl, or a plain-text file")
    private_ws(p); p.set_defaults(fn=cmd_scrub_check)

    p = sub.add_parser("scrub", help="a LOCAL model rewrites each hit into a case -> WS/mined/review-<date>.jsonl/.md")
    p.add_argument("--hits", required=True)
    p.add_argument("--model", default="qwen3:8b", help="Ollama model (loopback OLLAMA_HOST only)")
    private_ws(p); p.set_defaults(fn=cmd_scrub)

    p = sub.add_parser("accept", help="reviewed cases -> a copy of the skill's set under WS/sets/")
    p.add_argument("--review", required=True)
    private_ws(p); p.set_defaults(fn=cmd_accept)

    p = sub.add_parser("models", help="plan / run / report the anthropic: model comparison",
                       description=MODELS_HELP)
    p.add_argument("--models", nargs="+", help="anthropic:<model-id>@<effort> specs under test (judge and agent)")
    p.add_argument("--grader1", help="grading 1 of every canary run (not one of the models under test)")
    p.add_argument("--grader2", help="grading 2: a --regrade of the same replies")
    p.add_argument("--runs", type=_count, default=2, help="trigger runs per model and names mode (default 2)")
    p.add_argument("--reps", type=_count, default=3, help="canary reps per case (default 3)")
    p.add_argument("--sets", nargs="*", help="eval set files (default: sets/*.json)")
    p.add_argument("--canaries", default=str(ROOT / "canaries" / "overnight-runner.json"))
    p.add_argument("--skill-file", help="SKILL.md for the canary agent (default: the set's draft)")
    p.add_argument("--max-usd", type=float, help="with --go: refuse unless the plan total fits under it")
    p.add_argument("--go", action="store_true", help="run the plan (needs --workspace and --max-usd)")
    p.add_argument("--report", metavar="MANIFEST", help="rebuild the report from a plan manifest; no model call")
    p.set_defaults(fn=cmd_models)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
