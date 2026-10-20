"""Scorecards (one markdown file per skill per run), the run summary, HISTORY.md (every run
ever, one row per skill, so a drop shows up as a line that got worse), and `compare`
(which individual prompts flipped between two runs)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .trigger import Metrics, fmt


def load_rows(p: Path) -> list[dict]:
    if not Path(p).is_file():
        raise SystemExit(f"results file not found: {p}")
    return [json.loads(line) for line in Path(p).read_text(encoding="utf-8").splitlines() if line.strip()]


def trigger_rows(p: Path) -> list[dict]:
    """Rows of a trigger run. Canary rows (no `judge`) are not trigger results: skip them."""
    return [r for r in load_rows(p) if "judge" in r]


def _mark(r: dict) -> str:
    return {"TP": "ok", "TN": "ok", "FP": "WRONG (hijack)", "FN": "WRONG (missed)"}.get(r.get("outcome"), "ERROR")


def scorecard(skill: str, rows: list[dict], checks: list[dict]) -> str:
    m = Metrics.of(rows)
    r0 = rows[0] if rows else {}
    cost = sum(r.get("cost_usd", 0) for r in rows)
    lines = [
        f"# Scorecard: `{skill}`",
        "",
        f"- Run: `{r0.get('run_id', '-')}` · variant **{r0.get('variant', '-')}**",
        f"- Judge: **{r0.get('judge', '-')}**",
        f"- Description sha: `{r0.get('desc_sha')}` · catalogue sha: `{r0.get('catalogue_sha')}`",
        f"- Accepted as a hit: {', '.join(r0.get('accept', [skill]))}",
        "",
        "## Trigger eval",
        "",
        "| precision | recall | F1 | accuracy | TP | FP | TN | FN | errors | cost USD |",
        "|---|---|---|---|---|---|---|---|---|---|",
        f"| {fmt(m.precision)} | {fmt(m.recall)} | {fmt(m.f1)} | {fmt(m.accuracy)} | {m.tp} | {m.fp} | "
        f"{m.tn} | {m.fn} | {m.errors} | {cost:.4f} |",
        "",
        "| case | kind | expect | picked | result | prompt |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (r["case_id"], r.get("sample", 0))):
        p = r["prompt"].replace("|", "\\|")
        err = f" ({r['error']})" if r.get("error") else ""
        lines.append(f"| {r['case_id']} | {r.get('kind', '')} | {r['expect']} | {r.get('picked')} | "
                     f"{_mark(r)}{err} | {p} |")
    if checks:
        lines += ["", "## Deterministic checks", "", "| check | level | detail |", "|---|---|---|"]
        for c in checks:
            lines.append(f"| {c['check']} | {c['level']} | {c['detail'].replace('|', '/')} |")
    wrong = [r for r in rows if r.get("outcome") in ("FP", "FN")]
    if wrong:
        lines += ["", "## Misroutes to read", ""]
        for r in wrong:
            better = f" (the set expects `{r['better']}`)" if r.get("better") else ""
            lines.append(f"- `{r['case_id']}` {r['outcome']}: went to `{r.get('picked')}`{better}: \"{r['prompt']}\"")
    return "\n".join(lines) + "\n"


def write_scorecards(rows: list[dict], checks: list[dict], root: Path) -> Path:
    run_id = rows[0]["run_id"]
    out = root / "scorecards" / rows[0]["date"] / run_id
    out.mkdir(parents=True, exist_ok=True)
    by = defaultdict(list)
    for r in rows:
        by[r["skill"]].append(r)
    summary = [f"# Run `{run_id}`", "", f"Judge: **{rows[0]['judge']}** · variant **{rows[0]['variant']}**", "",
               "| skill | precision | recall | F1 | accuracy | errors | det. FAIL | det. WARN |",
               "|---|---|---|---|---|---|---|---|"]
    for skill, rs in sorted(by.items()):
        cs = [c for c in checks if c["skill"] == skill]
        (out / f"{skill}.md").write_text(scorecard(skill, rs, cs), encoding="utf-8")
        m = Metrics.of(rs)
        summary.append(f"| [{skill}]({skill}.md) | {fmt(m.precision)} | {fmt(m.recall)} | {fmt(m.f1)} | "
                       f"{fmt(m.accuracy)} | {m.errors} | {sum(c['level'] == 'FAIL' for c in cs)} | "
                       f"{sum(c['level'] == 'WARN' for c in cs)} |")
    cost = sum(r.get("cost_usd", 0) for r in rows)
    summary += ["", f"Total judge cost reported by the CLI: ${cost:.4f} (list-price basis)."]
    (out / "_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return out


def history(root: Path) -> Path:
    rows_out = []
    for p in sorted((root / "results").glob("*/*.jsonl")):
        rows = trigger_rows(p)
        if not rows:
            continue
        by = defaultdict(list)
        for r in rows:
            by[r["skill"]].append(r)
        for skill, rs in sorted(by.items()):
            m = Metrics.of(rs)
            rows_out.append(f"| {rs[0]['date']} | `{rs[0]['run_id']}` | {rs[0]['judge']} | {rs[0]['variant']} | "
                            f"{skill} | `{rs[0].get('desc_sha')}` | {fmt(m.precision)} | {fmt(m.recall)} | "
                            f"{fmt(m.f1)} | {m.errors} |")
    text = "\n".join([
        "# History (regenerated from results/*/*.jsonl — do not hand-edit)",
        "",
        "Compare rows with the SAME judge. A changed `desc sha` means the skill's description changed;",
        "a changed judge means the router model changed. Both are what you want to catch.",
        "",
        "| date | run | judge | variant | skill | desc sha | precision | recall | F1 | errors |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *rows_out,
    ]) + "\n"
    p = root / "HISTORY.md"
    p.write_text(text, encoding="utf-8")
    return p


def compare(a: list[dict], b: list[dict]) -> str:
    key = lambda r: (r["skill"], r["case_id"], r.get("sample", 0))  # noqa: E731
    ia = {key(r): r for r in a}
    lines = [f"# Compare `{a[0]['run_id']}` -> `{b[0]['run_id']}`", ""]
    skills = sorted({r["skill"] for r in a} | {r["skill"] for r in b})
    lines += ["| skill | F1 before | F1 after | delta |", "|---|---|---|---|"]
    regress = 0
    for s in skills:
        fa = Metrics.of([r for r in a if r["skill"] == s]).f1
        fb = Metrics.of([r for r in b if r["skill"] == s]).f1
        d = (fb - fa) if fa is not None and fb is not None else None
        if d is not None and d < 0:
            regress += 1
        lines.append(f"| {s} | {fmt(fa)} | {fmt(fb)} | {'n/a' if d is None else f'{d:+.2f}'} |")
    lines += ["", "## Flipped cases", ""]
    flips = 0
    for r in b:
        o = ia.get(key(r))
        if o and (o.get("outcome") in ("TP", "TN")) != (r.get("outcome") in ("TP", "TN")):
            flips += 1
            lines.append(f"- `{r['case_id']}` {o.get('outcome')} -> {r.get('outcome')} "
                         f"(picked {o.get('picked')} -> {r.get('picked')}): \"{r['prompt']}\"")
    if not flips:
        lines.append("none")
    lines += ["", f"REGRESSIONS: {regress} skill(s) lost F1 · {flips} case(s) flipped"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# REPORT.md: the one file a human reads. Built only from results/*/*.jsonl, never hand-edited.

INVALID_ERR_SHARE = 0.2  # a run where more than 20% of rows errored is not a measurement


def _runs(root: Path) -> list[dict]:
    runs = []
    for p in sorted((root / "results").glob("*/*.jsonl")):
        rows = trigger_rows(p)
        if not rows:
            continue
        m = Metrics.of(rows)
        variant = rows[0]["variant"]
        blind = bool(rows[0].get("blind_names")) or variant.endswith("-blind")
        runs.append({
            "run_id": rows[0]["run_id"], "judge": rows[0]["judge"], "variant": variant,
            "base": variant[:-len("-blind")] if variant.endswith("-blind") else variant,
            "blind": blind, "rows": rows, "m": m, "path": p,
            "cost": sum(r.get("cost_usd", 0) for r in rows),
            "invalid": m.errors > INVALID_ERR_SHARE * len(rows),
            "skills": sorted({r["skill"] for r in rows}),
        })
    return runs


def _err_reason(rows: list[dict]) -> str:
    errs = [r for r in rows if r.get("error")]
    if not errs:
        return ""
    raw = (errs[0].get("raw") or "").strip().splitlines()
    first = raw[0][:90] if raw else ""
    return f"{errs[0]['error']}" + (f" — \"{first}\"" if first else "")


def prove_triplets(runs: list[dict]) -> list[dict]:
    """baseline -> broken-* -> restored, same judge, same blind flag, same skills, in run order."""
    out = []
    for i, r in enumerate(runs):
        if r["base"] != "restored":
            continue
        same = lambda x: (x["judge"], x["blind"], x["skills"]) == (r["judge"], r["blind"], r["skills"])  # noqa: E731
        prev = [x for x in runs[:i] if same(x)]
        broken = next((x for x in reversed(prev) if x["base"].startswith("broken-")), None)
        base = next((x for x in reversed(prev) if x["base"] == "baseline"), None)
        if broken and base:
            out.append({"baseline": base, "broken": broken, "restored": r})
    return out


def prove_verdict(t: dict, min_drop: float = 0.3, tolerance: float = 0.15) -> str:
    if any(t[k]["invalid"] for k in ("baseline", "broken", "restored")):
        return "INCONCLUSIVE (a run errored)"
    fb, fx, fr = (t[k]["m"].f1 or 0.0 for k in ("baseline", "broken", "restored"))
    if fb - fx < min_drop:
        return f"FAIL (drop {fb - fx:+.2f} < {min_drop})"
    if fr < fb - tolerance:
        return "FAIL (did not recover)"
    return "PASS"


def summary(root: Path) -> str:
    runs = _runs(root)
    L = ["# skill-evals report (regenerated from results/*/*.jsonl — do not hand-edit)", ""]
    if not runs:
        return "\n".join(L + ["No runs yet."]) + "\n"
    L += [f"{len(runs)} run(s), {sum(len(r['rows']) for r in runs)} judge call(s), "
          f"reported judge cost ${sum(r['cost'] for r in runs):.4f} (CLI list-price basis; "
          "ollama is local and reports 0).", "",
          "Every judge is a PROXY for Claude Code's own skill router: it reads the same names and",
          "descriptions through a simulated router prompt. Compare scores only within one judge.", ""]

    L += ["## Latest full baseline per judge", ""]
    for judge in sorted({r["judge"] for r in runs}):
        cands = [r for r in runs if r["judge"] == judge and r["base"] == "baseline" and not r["invalid"]]
        if not cands:
            continue
        for blind in (False, True):
            pick = [r for r in cands if r["blind"] == blind]
            if not pick:
                continue
            best = max(pick, key=lambda r: (len(r["skills"]), r["run_id"]))
            L += [f"### {judge}{' · blind names' if blind else ''} — `{best['run_id']}`", "",
                  "| skill | precision | recall | F1 | TP | FP | TN | FN | errors |", "|---|---|---|---|---|---|---|---|---|"]
            by = defaultdict(list)
            for row in best["rows"]:
                by[row["skill"]].append(row)
            for s, rs in sorted(by.items()):
                m = Metrics.of(rs)
                L.append(f"| {s} | {fmt(m.precision)} | {fmt(m.recall)} | {fmt(m.f1)} | {m.tp} | {m.fp} | "
                         f"{m.tn} | {m.fn} | {m.errors} |")
            m = best["m"]
            L += [f"| **all** | {fmt(m.precision)} | {fmt(m.recall)} | {fmt(m.f1)} | {m.tp} | {m.fp} | "
                  f"{m.tn} | {m.fn} | {m.errors} |", ""]
            wrong = [r for r in best["rows"] if r.get("outcome") in ("FP", "FN")]
            if wrong:
                L += ["Misroutes:", ""]
                for r in sorted(wrong, key=lambda r: r["case_id"]):
                    better = f", set expects `{r['better']}`" if r.get("better") else ""
                    L.append(f"- `{r['case_id']}` {r['outcome']} → `{r.get('picked')}`{better}: \"{r['prompt']}\"")
                L.append("")

    trip = prove_triplets(runs)
    if trip:
        L += ["## Prove-can-fail (break a description in memory, restore it)", "",
              "| judge | names | skill(s) | F1 baseline | F1 broken | F1 restored | verdict |", "|---|---|---|---|---|---|---|"]
        for t in trip:
            L.append(f"| {t['restored']['judge']} | {'blind' if t['restored']['blind'] else 'real'} | "
                     f"{', '.join(t['restored']['skills'])} | {fmt(t['baseline']['m'].f1)} | "
                     f"{fmt(t['broken']['m'].f1)} | {fmt(t['restored']['m'].f1)} | {prove_verdict(t)} |")
        L.append("")

    bad = [r for r in runs if r["invalid"]]
    if bad:
        L += ["## Invalid runs (more than 20% of rows errored — not a measurement)", ""]
        for r in bad:
            L.append(f"- `{r['run_id']}`: {r['m'].errors}/{len(r['rows'])} errored · {_err_reason(r['rows'])}")
        L.append("")

    L += ["## All runs", "", "| run | judge | variant | skills | rows | F1 | errors | cost USD |",
          "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        L.append(f"| `{r['run_id']}` | {r['judge']} | {r['variant']} | {len(r['skills'])} | {len(r['rows'])} | "
                 f"{fmt(r['m'].f1)} | {r['m'].errors} | {r['cost']:.4f} |")
    return "\n".join(L) + "\n"


def write_summary(root: Path) -> Path:
    p = root / "REPORT.md"
    p.write_text(summary(root), encoding="utf-8")
    return p
