"""Canary regression gate: reads committed canary results, makes no model call, and exits non-zero
when a run after the pinned baseline regressed. CI runs it on every push and pull request.

The pinned baseline is the canary set's `baseline_run`: a fresh run whose every row passed under
at least two gradings (the run and a `--regrade` of it). Every later fresh run of the same skill and
agent is a candidate, so a bad run cannot be hidden by committing a good one after it.

A grader is one noisy model call per case, so the gate never trusts one grading. Per (case, rep):

- pass        every grading that graded the row passed it, and there are at least two
- fail        every grading failed it, and there are at least two (a confirmed fail)
- disputed    the gradings disagree: reported, not a failure (grader noise is not agent evidence)
- undergraded fewer than two gradings graded it (errors count as no grading)

Exit 1 when any candidate row is a confirmed fail. Otherwise exit 2 when the result cannot be
trusted: the baseline is not certified, a candidate misses rows or has undergraded rows, or the
skill file changed after the newest run. Otherwise exit 0.
"""
from __future__ import annotations

from pathlib import Path

from . import report

PASS, FAIL, DISPUTED, UNDERGRADED = "pass", "fail", "disputed", "undergraded"


def load_runs(root: Path) -> dict[str, list[dict]]:
    """Every canary results file under root/results/canary, keyed by run_id."""
    runs: dict[str, list[dict]] = {}
    for p in sorted((root / "results" / "canary").glob("*/*.jsonl")):
        for r in report.load_rows(p):
            runs.setdefault(r["run_id"], []).append(r)
    return runs


def gradings(runs: dict[str, list[dict]], run_id: str) -> list[list[dict]]:
    """The fresh run's own grading, then every regrade of it, oldest first."""
    regrades = sorted(k for k, rows in runs.items() if rows[0].get("regraded_from") == run_id)
    return [runs[run_id]] + [runs[k] for k in regrades]


def row_status(grades: list[list[dict]]) -> dict[tuple[str, int], str]:
    """(case_id, rep) -> pass | fail | disputed | undergraded, over every row of the fresh run."""
    keys = [(r["case_id"], r["rep"]) for r in grades[0]]
    votes: dict[tuple[str, int], list[bool]] = {k: [] for k in keys}
    for g in grades:
        for r in g:
            k = (r["case_id"], r["rep"])
            if k in votes and not r.get("error") and r.get("passed") is not None:
                votes[k].append(bool(r["passed"]))
    out = {}
    for k, v in votes.items():
        if len(v) < 2:
            out[k] = UNDERGRADED
        elif all(v):
            out[k] = PASS
        elif not any(v):
            out[k] = FAIL
        else:
            out[k] = DISPUTED
    return out


def check(root: Path, skill: str, baseline_run: str, current_sha: str) -> tuple[int, list[str]]:
    """(exit code, report lines) for one canary set."""
    runs = load_runs(root)
    if baseline_run not in runs:
        return 2, [f"INCONCLUSIVE: baseline run {baseline_run} not found under {root / 'results' / 'canary'}"]
    base = runs[baseline_run][0]
    lines: list[str] = []
    inconclusive: list[str] = []
    regressed: list[str] = []

    base_status = row_status(gradings(runs, baseline_run))
    bad = sorted(f"{c} rep{n}" for (c, n), s in base_status.items() if s != PASS)
    lines.append(f"baseline {baseline_run}: {len(base_status)} rows, "
                 f"{len(gradings(runs, baseline_run))} gradings, "
                 f"{'certified' if not bad else 'NOT certified'}")
    if bad:
        inconclusive.append(f"baseline not certified: {', '.join(bad)}")

    cands = sorted(k for k, rows in runs.items()
                   if k > baseline_run and not rows[0].get("regraded_from")
                   and rows[0]["skill"] == skill and rows[0]["agent"] == base["agent"])
    for run_id in cands:
        g = gradings(runs, run_id)
        st = row_status(g)
        n = {s: sum(v == s for v in st.values()) for s in (PASS, FAIL, DISPUTED, UNDERGRADED)}
        lines.append(f"candidate {run_id}: {len(st)} rows, {len(g)} gradings, pass {n[PASS]}, "
                     f"fail {n[FAIL]}, disputed {n[DISPUTED]}, undergraded {n[UNDERGRADED]}")
        for (c, rep), s in sorted(st.items()):
            if s == FAIL:
                regressed.append(f"{run_id} {c} rep{rep}")
                lines.append(f"  REGRESSION {c} rep{rep}: failed under every grading")
            elif s == DISPUTED:
                lines.append(f"  disputed   {c} rep{rep}: gradings disagree (grader noise, not counted)")
        missing = sorted(set(base_status) - set(st))
        if missing:
            inconclusive.append(f"{run_id} misses {len(missing)} baseline rows")
        if n[UNDERGRADED]:
            inconclusive.append(f"{run_id} has {n[UNDERGRADED]} rows with fewer than two gradings "
                                f"(run `canary --regrade` on it)")

    newest = runs[cands[-1] if cands else baseline_run][0]
    if newest.get("skill_sha") != current_sha:
        inconclusive.append(f"skill file changed since the newest run ({newest.get('skill_sha')} -> "
                            f"{current_sha}): rerun the canaries and regrade")

    if regressed:
        lines.append(f"GATE: FAIL  {len(regressed)} confirmed regression(s)")
        code = 1
    elif inconclusive:
        lines.append("GATE: INCONCLUSIVE  " + "; ".join(inconclusive))
        code = 2
    else:
        lines.append(f"GATE: PASS  {len(cands)} candidate run(s) after the baseline, no confirmed regression")
        code = 0
    if regressed and inconclusive:
        lines.append("also: " + "; ".join(inconclusive))
    return code, lines
