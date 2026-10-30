"""The three-model comparison report (docs/design-api-backend.md section 2), rebuilt from a plan
manifest written by `models --go`. The manifest lists every planned run and, once it finished,
its results file (relative to the workspace), so the report reads only results files.

Every run is listed (no best-of). Costs are summed per basis and never added across bases. Two
gradings of one canary run are matched by (case_id, rep): rows that errored in the source run
were never regraded, so they are left out of the agreement count and reported on their own."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from . import behaviour, report
from .trigger import Metrics, fmt

CANARY_PAIRS = (("agent_cost_usd", "agent_cost_basis"), ("grader_cost_usd", "grader_cost_basis"))
COST_PAIRS = {"trigger": (("cost_usd", "cost_basis"),), "canary": CANARY_PAIRS, "regrade": CANARY_PAIRS}


def agreement(src: list[dict], reg: list[dict]) -> dict:
    """Rows of the source run graded by both gradings that agree on behaviour AND on passed.
    A source row that errored was not regraded: it is excluded and counted apart. A regrade row
    that errored (or is missing) is a disagreement."""
    graded = [r for r in src if not r["error"]]
    by = {(r["case_id"], r["rep"]): r for r in reg}
    agree = 0
    for r in graded:
        g = by.get((r["case_id"], r["rep"]))
        if g is not None and not g["error"] and g["behaviour"] == r["behaviour"] and g["passed"] == r["passed"]:
            agree += 1
    return {"graded": len(graded), "agree": agree, "excluded": len(src) - len(graded)}


def certify(result1: str, result2: str, agr: dict) -> str:
    """A canary PASS or FAIL counts only when both gradings give it AND agree on every graded row.
    Anything else (INCONCLUSIVE, a missing grading, a disagreement) is "not certified"."""
    if result1 == result2 and result1 in ("PASS", "FAIL") and agr["agree"] == agr["graded"]:
        return result1
    return "not certified"


def _costs(rows: list[dict], pairs) -> dict[str, float]:
    return report.cost_by_basis(rows, pairs)


def _add(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = round(out.get(k, 0.0) + v, 6)
    return out


def _cell(x) -> str:
    return str(x).replace("|", "/")


def render(m: dict, root: Path) -> str:
    steps = m["steps"]
    rows = [report.load_rows(root / s["results"]) if s.get("results") else [] for s in steps]
    L = [f"# Model comparison `{m['plan_id']}` (regenerated from its plan manifest — do not hand-edit)", "",
         f"Models: {', '.join(f'`{x}`' for x in m['models'])} · grading 1 `{m['grader1']}` · grading 2 "
         f"`{m['grader2']}` · {m['runs']} trigger run(s) per names mode · canary reps {m['reps']} · "
         f"API prices as of {m['prices_as_of']} · --max-usd ${m['max_usd']}", ""]
    if m.get("stopped"):
        L += [f"**Plan stopped:** {m['stopped']}. The steps marked `not run` never started.", ""]

    trig = [(s, rs) for s, rs in zip(steps, rows) if s["kind"] == "trigger"]
    skills = sorted({r["skill"] for _, rs in trig for r in rs})
    L += ["## Trigger judge (every run)", "",
          "| model | run | names | status | rows | errors | " + "".join(f"F1 {k} | " for k in skills)
          + "cost | median latency s |",
          "|---|---|---|---|---|---|" + "---|" * len(skills) + "---|---|"]
    for s, rs in trig:
        if not rs:
            L.append(f"| {s['model']} | {s['run']} | {s['names']} | {s['status']} | 0 | - | "
                     + "- | " * len(skills) + "- | - |")
            continue
        f1 = [fmt(Metrics.of([r for r in rs if r["skill"] == k]).f1) if any(r["skill"] == k for r in rs) else "-"
              for k in skills]
        lat = [r["latency_s"] for r in rs if r.get("error") != "budget cap reached"]
        L.append(f"| {s['model']} | {s['run']} | {s['names']} | {s['status']} | {len(rs)} | {Metrics.of(rs).errors} | "
                 + "".join(f"{x} | " for x in f1)
                 + f"{report.fmt_costs(_costs(rs, COST_PAIRS['trigger']))} | "
                 + (f"{statistics.median(lat):.2f}" if lat else "-") + " |")
    L.append("")

    L += ["## Canary agent (every run, two gradings)", "",
          "Agree = graded rows where both gradings give the same behaviour AND the same passed "
          "(matched by case and rep). Excluded = rows that errored in the source run (never regraded). "
          "Certified only when both gradings give the same result and agree on every graded row.", "",
          "| agent | status | rows | errors | grading 1 | grading 2 | agree | excluded | certified | "
          "agent $ | grader 1 $ | grader 2 $ |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(steps):
        if s["kind"] != "canary":
            continue
        src = rows[i]
        j = next((k for k in range(i + 1, len(steps)) if steps[k]["kind"] == "regrade"
                  and steps[k]["model"] == s["model"]), None)
        reg, rstat = (rows[j], steps[j]["status"]) if j is not None else ([], "not planned")
        v1 = behaviour.verdict(src)["result"] if src else "-"
        v2 = behaviour.verdict(reg)["result"] if reg else f"- ({rstat})"
        if src and reg:
            agr = agreement(src, reg)
            agree, cert = f"{agr['agree']}/{agr['graded']}", certify(v1, v2, agr)
            excl = agr["excluded"]
        else:
            agree, cert, excl = "-", "not certified", (sum(bool(r["error"]) for r in src) if src else "-")
        L.append(f"| {s['model']} | {s['status']} | {len(src)} | {sum(bool(r['error']) for r in src)} | {v1} | "
                 f"{_cell(v2)} | {agree} | {excl} | {cert} | "
                 f"{report.fmt_costs(_costs(src, (CANARY_PAIRS[0],)))} | "
                 f"{report.fmt_costs(_costs(src, (CANARY_PAIRS[1],)))} | "
                 f"{report.fmt_costs(_costs(reg, (CANARY_PAIRS[1],)))} |")
    L.append("")

    L += ["## Spend: ceiling (worst case) vs real", "",
          "| step | kind | model | names / grader | status | ceiling | real |", "|---|---|---|---|---|---|---|"]
    total_real: dict[str, float] = {}
    by_kind: dict[str, list] = {}
    unknown = 0
    for n, (s, rs) in enumerate(zip(steps, rows), 1):
        real = _costs(rs, COST_PAIRS[s["kind"]])
        total_real = _add(total_real, real)
        # stopped before its results file existed: calls may have been billed with no row to show it
        paid_unknown = s["status"] in ("crashed", "interrupted") and not s.get("results")
        unknown += paid_unknown
        k = by_kind.setdefault(s["kind"], [0.0, {}])
        k[0] += s["worst_usd"]
        k[1] = _add(k[1], real)
        who = s.get("names") or s.get("grader") or ""
        run = f" run {s['run']}" if s.get("run") else ""
        spent = f"unknown (up to ${s['worst_usd']:.4f})" if paid_unknown else report.fmt_costs(real)
        L.append(f"| {n} | {s['kind']}{run} | {s['model']} | {who} | {s['status']} | ${s['worst_usd']:.4f} | "
                 f"{spent} |")
    for kind, (ceil, real) in by_kind.items():
        L.append(f"| | **{kind} total** | | | | ${ceil:.4f} | {report.fmt_costs(real)} |")
    L.append(f"| | **plan total** | | | | ${sum(s['worst_usd'] for s in steps):.4f} | "
             f"{report.fmt_costs(total_real)}"
             + (f" (lower bound: {unknown} step(s) stopped before writing results; spend unknown)" if unknown else "")
             + " |")
    return "\n".join(L) + "\n"


def report_path(m: dict, root: Path) -> Path:
    return root / "reports" / f"{m['plan_id']}-models.md"


def write_report(manifest: Path, root: Path | None = None) -> Path:
    """root: the workspace. Default: the one the manifest sits in (<ws>/results/models/<id>.json)."""
    if not Path(manifest).is_file():
        raise SystemExit(f"plan manifest not found: {manifest}")
    m = json.loads(Path(manifest).read_text(encoding="utf-8"))
    root = root or Path(manifest).resolve().parents[2]
    p = report_path(m, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(m, root), encoding="utf-8")
    return p
