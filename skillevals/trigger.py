"""Trigger evals: does the router pick this skill when it should, and leave it alone when it
should not?

Each case is one user prompt labelled `trigger` (the skill SHOULD be picked) or `no` (it
should NOT). The judge sees the whole catalogue and names one skill or none. Four outcomes:

    expect trigger, picked it  -> TP (true positive)
    expect trigger, missed     -> FN (false negative: the skill never fires)
    expect no,      picked it  -> FP (false positive: the skill hijacks someone else's prompt)
    expect no,      left it    -> TN (true negative)

precision = TP / (TP + FP)   "when it fires, is it right?"
recall    = TP / (TP + FN)   "when it should fire, does it?"
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import catalogue as cat_mod
from .judges import ROUTER_SYSTEM


def load_set(path: Path) -> dict:
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    for c in s["cases"]:
        assert c["expect"] in ("trigger", "no"), f"{c['id']}: expect must be trigger|no"
    s.setdefault("accept", [s["skill"]])
    return s


def outcome(expect: str, picked: str | None, accept: list[str]) -> str | None:
    if picked is None:
        return None
    hit = picked in accept
    if expect == "trigger":
        return "TP" if hit else "FN"
    return "FP" if hit else "TN"


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    errors: int = 0

    @staticmethod
    def of(rows: list[dict]) -> "Metrics":
        m = Metrics()
        for r in rows:
            o = r.get("outcome")
            if o is None:
                m.errors += 1
            else:
                setattr(m, o.lower(), getattr(m, o.lower()) + 1)
        return m

    @property
    def precision(self):
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self):
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self):
        p, r = self.precision, self.recall
        if p is None and r is None:
            return None
        p, r = p or 0.0, r or 0.0
        return 0.0 if p + r == 0 else 2 * p * r / (p + r)

    @property
    def accuracy(self):
        n = self.tp + self.fp + self.tn + self.fn
        return (self.tp + self.tn) / n if n else None


def fmt(x) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def run(sets: list[dict], judge, catalogue: dict, run_id: str, variant: str = "baseline",
        max_usd: float = 1.0, samples: int = 1, progress=print, blind: bool = False) -> list[dict]:
    """blind=True hides skill NAMES behind opaque ids (skill-01, ...), so the judge can only
    route on the descriptions. Use it to measure a description on its own: with real names,
    a prompt that says "commit" finds `gen-commits` even if its description is garbage."""
    shown = {n: (f"skill-{i:02d}" if blind else n) for i, n in enumerate(catalogue, 1)}
    back = {v: k for k, v in shown.items()}
    system = ROUTER_SYSTEM.format(catalogue="\n".join(
        f"- {shown[s.name]}: {s.description}" for s in catalogue.values()))
    cat_sha = cat_mod.catalogue_sha(catalogue)
    names = set(back)
    jobs = [(s, c, k) for s in sets for c in s["cases"] for k in range(samples)]
    spent = [0.0]
    lock = threading.Lock()
    date = run_id[:10]

    def one(job):
        s, c, k = job
        with lock:
            over = spent[0] >= max_usd
        row = {
            "run_id": run_id, "date": date, "judge": judge.label, "variant": variant,
            "skill": s["skill"], "accept": s["accept"], "case_id": c["id"], "sample": k,
            "blind_names": blind, "kind": c.get("kind", ""), "prompt": c["prompt"], "expect": c["expect"],
            "better": c.get("better"),
            "desc_sha": cat_mod.sha(catalogue[s["skill"]].description) if s["skill"] in catalogue else None,
            "catalogue_sha": cat_sha,
        }
        if over:
            row.update(picked=None, outcome=None, error="budget cap reached", latency_s=0, cost_usd=0)
            return row
        v = judge(system, c["prompt"], names)
        if v.picked not in (None, "none"):
            v.picked = back[v.picked]
        with lock:
            spent[0] += v.cost_usd
        row.update(picked=v.picked, outcome=outcome(c["expect"], v.picked, s["accept"]),
                   error=v.error, latency_s=round(v.latency_s, 2), cost_usd=round(v.cost_usd, 6),
                   raw=(v.raw or "")[:200])
        progress(f"  {row['case_id']:<10} expect={c['expect']:<7} picked={str(v.picked):<22} "
                 f"{row['outcome'] or 'ERR ' + str(v.error)}")
        return row

    with ThreadPoolExecutor(max_workers=getattr(judge, "workers", 1)) as ex:
        rows = list(ex.map(one, jobs))
    return rows


def new_run_id(judge_label: str, variant: str) -> str:
    slug = judge_label.split(" ")[0].replace(":", "-")
    return f"{datetime.now():%Y-%m-%dT%H%M%S}-{slug}-{variant}"


def write_jsonl(rows: list[dict], root: Path) -> Path:
    d = root / "results" / rows[0]["date"]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rows[0]['run_id']}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p
