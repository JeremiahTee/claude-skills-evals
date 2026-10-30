"""Red cases from your own transcripts, the local half (docs/design-api-backend.md section 4).

    mine         read Claude Code transcripts (JSONL), flag routing misses -> WS/mined/hits-<date>.jsonl
    scrub_check  deterministic secret scanner + deny list; no model
    scrub        a LOCAL model (Ollama on loopback only) rewrites each hit into a case, then
                 scrub_check runs on the draft -> WS/mined/review-<date>.jsonl + .md
    accept       the owner's reviewed rows join a COPY of the skill's set under WS/sets/

Raw transcript text stays on this machine: it is read here, shown to a loopback model for the
rewrite, and written only under the workspace. Nothing in this module calls a remote model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- transcripts

# Not verified against a spec: the text Claude Code writes as a user turn when a run is stopped.
INTERRUPT_MARKER = "[Request interrupted by user"

# A correction is the NEXT typed user turn after a skill fired, and must match one of these.
# Kept small and conservative on purpose: a missed correction costs one case, a false one costs
# review time and a wrong red case. "no" counts only as a whole reply or before punctuation, so
# "no worries" / "no problem" / "nothing" / "noted" do not match.
CORRECTION_PATTERNS = [
    r"^\s*(no|nope)\s*([,.!;:]|$)",          # "no, ..." / "No." / "nope"
    r"^\s*not that\b",                        # "not that one"
    r"^\s*i meant\b",                         # "I meant the other thing"
    r"\bwrong skill\b",                       # anywhere
    r"\bthat'?s not what i (asked|meant|wanted)\b",
]
_CORRECTION = [re.compile(p, re.I) for p in CORRECTION_PATTERNS]
_COMMAND = re.compile(r"<command-name>/?([A-Za-z0-9_.:-]+)</command-name>")
MESSAGE_TYPES = ("user", "assistant")


def is_correction(text: str) -> bool:
    return any(p.search(text) for p in _CORRECTION)


def _typed_text(content) -> str | None:
    """The text the user typed in a user line, or None when the line is tool output only.
    tool_result blocks are raw text too, but never a prompt, a correction or a command."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def _skills_fired(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [str(b["input"]["skill"]) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
            and isinstance(b.get("input"), dict) and b["input"].get("skill")]


def _events(path: Path, stats: dict):
    """Yield (line_no, kind, payload, line) for the main chain of one transcript file.
    kind: prompt | command | interrupt | skill. Everything else is skipped and counted."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            if not isinstance(d, dict):
                stats["malformed"] += 1
                continue
            if d.get("type") not in MESSAGE_TYPES:
                stats["other_types"] += 1
                continue
            msg = d.get("message")
            if not isinstance(msg, dict) or "content" not in msg:
                stats["malformed"] += 1
                continue
            if d.get("isSidechain"):
                stats["sidechain"] += 1  # a subagent's turn: never a prompt, never a signal
                continue
            if d.get("isMeta"):
                stats["meta"] += 1  # text the harness injected (e.g. a skill body), not typed
                continue
            if d["type"] == "assistant":
                for s in _skills_fired(msg["content"]):
                    yield n, "skill", s, d
                continue
            text = _typed_text(msg["content"])
            if text is None:
                stats["tool_output"] += 1
                continue
            if text.lstrip().startswith(INTERRUPT_MARKER):
                yield n, "interrupt", text, d
                continue
            m = _COMMAND.search(text)
            if m:
                yield n, "command", m.group(1), d
            elif text.lstrip().startswith("<"):
                stats["harness_text"] += 1  # <local-command-stdout>, <system-reminder>, ...
            else:
                yield n, "prompt", text.strip(), d


def _hit(signal, skill, prompt, root: Path, path: Path, evidence: int) -> dict:
    line, text, d = prompt
    src = path.relative_to(root).as_posix()
    hid = hashlib.sha256(f"{src}:{line}:{signal}:{skill}".encode()).hexdigest()[:12]
    return {"id": hid, "signal": signal, "skill": skill, "prompt": text, "source": src, "line": line,
            "evidence_line": evidence, "timestamp": d.get("timestamp"), "session_id": d.get("sessionId")}


def mine_file(path: Path, root: Path, known: set[str], stats: dict) -> list[dict]:
    hits: list[dict] = []
    prompt = None   # (line, text, row) of the last typed prompt, or None after a slash command
    fired = None    # skill fired since that prompt, waiting for the next user turn
    for n, kind, val, d in _events(path, stats):
        if kind == "skill":
            if prompt and fired is None:
                fired = val
            continue
        if kind == "interrupt":
            if prompt and fired:
                hits.append(_hit("interrupted", fired, prompt, root, path, n))
                fired = None  # one hit per run
            continue
        if kind == "command":
            if prompt and fired is None and val in known:
                hits.append(_hit("missed", val, prompt, root, path, n))
            prompt, fired = None, None  # the user routed it by hand: not a routing event
            continue
        # a typed prompt: it may correct the skill that just fired, then it starts a new turn
        if prompt and fired and is_correction(val):
            hits.append(_hit("corrected", fired, prompt, root, path, n))
        prompt, fired = (n, val, d), None
    return hits


def new_stats() -> dict:
    return {"files": 0, "malformed": 0, "other_types": 0, "sidechain": 0, "meta": 0,
            "tool_output": 0, "harness_text": 0, "unreadable_files": 0}


def mine(root: Path, known: set[str], since: str | None = None) -> tuple[list[dict], dict]:
    root = Path(root)
    stats = new_stats()
    hits: list[dict] = []
    for p in sorted(root.rglob("*.jsonl")):
        stats["files"] += 1
        try:
            hits += mine_file(p, root, known, stats)
        except OSError:
            stats["unreadable_files"] += 1
    if since:
        hits = [h for h in hits if (h.get("timestamp") or "")[:10] >= since]
    return hits, stats


# ---------------------------------------------------------------- scrub check

# Secrets first, then the deny list. Each match reports its rule and a masked hint, never the text.
SECRET_RULES = [
    ("secret:anthropic-key", r"sk-ant-[A-Za-z0-9_-]{8,}"),
    ("secret:api-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("secret:aws-access-key", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("secret:private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("secret:github-token", r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("secret:slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("secret:jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
    ("secret:bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}"),
    ("secret:assignment", r"(?i)\b(api[_-]?key|token|secret|passw(or)?d|pwd)\s*[:=]\s*\S{6,}"),
]
_TLDS = ("com|net|org|io|dev|ai|app|co|ca|uk|us|de|fr|eu|me|info|biz|xyz|cloud|local|internal|"
         "lan|tech|site|online|gg|tv|ly")
DENY_RULES = [
    ("deny:email", r"[\w.+-]+@[\w-]+(\.[\w-]+)+"),
    ("deny:url", r"(?i)\b([a-z][a-z0-9+.-]*://\S+|www\.\S+)"),
    ("deny:ip-address", r"\b\d{1,3}(\.\d{1,3}){3}\b"),
    ("deny:hostname", rf"(?i)\b([a-z0-9-]+\.)+({_TLDS})\b|\blocalhost\b"),
    ("deny:phone", r"(?<![\w.])(\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    ("deny:path", r"(?<![\w/])(~|\.\.?)?/[\w.-]+/[\w./-]*|(?<![\w/])~/[\w.-]+|\b[A-Za-z]:\\\S+"),
    ("deny:money", r"(?i)[$€£¥]\s?\d|\d\s?[$€£¥]|\b\d+(?:[.,]\d+)?\s?(usd|cad|eur|gbp|dollars?|bucks)\b"),
]
_RULES = [(name, re.compile(p)) for name, p in SECRET_RULES + DENY_RULES]


def _mask(s: str) -> str:
    return f"{s[:2]}... ({len(s)} chars)" if len(s) > 4 else f"({len(s)} chars)"


def scrub_check(text: str, private_names=()) -> list[dict]:
    """[] when the text is clean; else one {"rule", "hint"} per rule that matched (hint masked)."""
    out = []
    for name, rx in _RULES:
        m = rx.search(text)
        if m:
            hint = f"({len(m.group(0))} chars)" if name.startswith("secret:") else _mask(m.group(0))
            out.append({"rule": name, "hint": hint})
    for nm in private_names:
        m = re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", text, re.I)
        if m:
            out.append({"rule": "deny:private-name", "hint": _mask(m.group(0))})
            break
    return out


def private_names(ws: Path | None) -> list[str]:
    """WS/private-names.txt: one name per line; blank lines and # comments ignored."""
    if ws is None:
        return []
    p = Path(ws) / "private-names.txt"
    if not p.is_file():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def check_texts(file: Path) -> list[tuple[str, str]]:
    """(label, text) pairs to check from a set .json, a review/hits .jsonl, or plain text."""
    file = Path(file)
    raw = file.read_text(encoding="utf-8")
    if file.suffix == ".json":
        d = json.loads(raw)
        return [(c.get("id", f"case-{i}"), c["prompt"]) for i, c in enumerate(d["cases"])]
    if file.suffix == ".jsonl":
        out = []
        for i, line in enumerate(raw.splitlines(), 1):
            if line.strip():
                r = json.loads(line)
                out.append((str(r.get("id", f"line-{i}")), case_prompt(r) or r.get("prompt", "")))
        return out
    return [(file.name, raw)]


# ---------------------------------------------------------------- scrub (local model)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_HOST = "http://127.0.0.1:11434"


def check_host(host: str | None) -> str:
    """OLLAMA_HOST -> a base URL, or SystemExit unless it is loopback (0.0.0.0 is refused)."""
    h = (host or "").strip() or DEFAULT_HOST
    if "://" not in h:
        h = "http://" + h
    u = urllib.parse.urlsplit(h)
    try:
        name = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        name, port = "", None
    if u.scheme not in ("http", "https") or name not in LOOPBACK:
        raise SystemExit(f"refusing OLLAMA_HOST={host!r}: the scrub model sees raw transcript text, "
                         "so it must be a loopback address (127.0.0.1, localhost or ::1)")
    netloc = f"[{name}]" if ":" in name else name
    return f"{u.scheme}://{netloc}:{port or 11434}"


SCRUB_SYSTEM = """You rewrite a user's message to an AI coding assistant into a short, generic
test prompt for a skill router. Keep what the user was asking the assistant to DO (the task
type, so the same skill would or would not fit). Remove everything personal: names of people,
companies, products, places, dates, amounts of money, file paths, URLs, emails, hostnames,
phone numbers, keys, and any quoted message text. Replace them with generic words ("a teammate",
"the project", "a file"). Do not add new facts. Reply with JSON only: {"prompt": "<rewritten>"}"""


class ScrubError(Exception):
    pass


class OllamaScrubber:
    """Calls a local Ollama model. The host is checked again here, so no caller can skip it.
    It never starts or stops a server: start `ollama serve` (bound to 127.0.0.1) yourself."""

    def __init__(self, model: str, host: str, timeout: int = 300):
        self.model, self.host, self.timeout = model, check_host(host), timeout

    def __call__(self, prompt: str, skill: str, signal: str) -> str:
        body = json.dumps({
            "model": self.model, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0}, "keep_alive": "10m",
            "messages": [{"role": "system", "content": SCRUB_SYSTEM},
                         {"role": "user", "content": f"Skill involved: {skill}\nMessage:\n{prompt}"}],
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = json.loads(r.read()).get("message", {}).get("content", "")
        except Exception as e:  # noqa: BLE001 - any transport failure is a rejected row
            raise ScrubError(f"ollama: {e}") from e
        try:
            out = json.loads(text)["prompt"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ScrubError("unparseable model reply") from e
        if not isinstance(out, str) or not out.strip():
            raise ScrubError("empty model reply")
        return out.strip()


# signal -> proposed expect. corrected/interrupted: the skill fired and should not have.
# missed: the skill should have fired. The owner can change it in review.
PROPOSED_EXPECT = {"corrected": "no", "interrupted": "no", "missed": "trigger"}


def _today() -> str:
    return dt.date.today().isoformat()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in Path(p).read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(p: Path, rows: list[dict]) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def scrub(hits_file: Path, ws: Path, model_call) -> tuple[list[dict], Path]:
    names = private_names(ws)
    rows = []
    for h in read_jsonl(hits_file):
        row = {"id": h["id"], "skill": h["skill"], "signal": h["signal"], "raw_prompt": h["prompt"],
               "source": h.get("source"), "line": h.get("line"), "timestamp": h.get("timestamp")}
        try:
            draft = model_call(h["prompt"], h["skill"], h["signal"])
            found = [m["rule"] for m in scrub_check(draft, names)]
        except ScrubError as e:
            draft, found = None, ["model:error"]
            row["model_error"] = str(e)[:200]
        row.update({"scrubbed_prompt": draft, "expect": PROPOSED_EXPECT.get(h["signal"], "no"),
                    "scrub_check": {"ok": not found, "rules": found},
                    "status": "pending" if not found else "rejected"})
        rows.append(row)
    out = write_jsonl(Path(ws) / "mined" / f"review-{_today()}.jsonl", rows)
    out.with_suffix(".md").write_text(render_review(rows), encoding="utf-8")
    return rows, out


def _quote(t: str | None) -> str:
    return "\n".join("> " + ln for ln in (t or "(none)").splitlines()) or "> "


def render_review(rows: list[dict]) -> str:
    out = ["# Mined cases to review", "",
           "Raw text stays in this workspace. Set each row's `status` in the .jsonl to `accepted`, "
           "`edited` (and add `edited_prompt`) or `dropped`, then run `skillevals accept`.", ""]
    for r in rows:
        chk = "ok" if r["scrub_check"]["ok"] else "REJECTED: " + ", ".join(r["scrub_check"]["rules"])
        out += [f"## {r['id']} · {r['skill']} · {r['signal']} · expect {r['expect']} · {r['status']}", "",
                f"scrub check: {chk}", "", "Raw:", "", _quote(r["raw_prompt"]), "",
                "Scrubbed:", "", _quote(r["scrubbed_prompt"]), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- accept

STATUSES = ("pending", "rejected", "accepted", "edited", "dropped")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def case_prompt(r: dict) -> str | None:
    """The prompt a review row would contribute: the owner's edit if any, else the scrubbed draft."""
    return r.get("edited_prompt") or r.get("scrubbed_prompt")


def accept(review: Path, ws: Path, public_sets: Path) -> dict[str, int]:
    """Check every row first; write nothing unless all pass. Returns cases added per skill."""
    rows = read_jsonl(review)
    names = private_names(ws)
    problems, take = [], []
    for r in rows:
        st = r.get("status")
        if st not in STATUSES:
            problems.append(f"{r.get('id')}: status {st!r} is not one of {', '.join(STATUSES)}")
            continue
        # an edited prompt is checked whatever its status: the owner typed it, it may leak
        if st in ("accepted", "edited") or r.get("edited_prompt"):
            text = r.get("edited_prompt") or (r.get("scrubbed_prompt") if st == "accepted" else None)
            if not text:
                problems.append(f"{r.get('id')}: status {st} but no prompt"
                                + (" (add edited_prompt)" if st == "edited" else ""))
                continue
            found = scrub_check(text, names)
            if found:
                problems.append(f"{r.get('id')}: " + ", ".join(f"{m['rule']} {m['hint']}" for m in found))
                continue
        if st not in ("accepted", "edited"):
            continue
        if r.get("expect") not in ("trigger", "no"):
            problems.append(f"{r.get('id')}: expect must be trigger|no, not {r.get('expect')!r}")
            continue
        if not _SKILL_NAME.match(str(r.get("skill", ""))) or ".." in str(r.get("skill")):
            problems.append(f"{r.get('id')}: bad skill name")
            continue
        take.append(r)
    if problems:
        raise SystemExit("accept refused, nothing written:\n  " + "\n  ".join(problems))
    added: dict[str, int] = {}
    for skill in sorted({r["skill"] for r in take}):
        dest = Path(ws) / "sets" / f"{skill}.json"
        src = dest if dest.is_file() else Path(public_sets) / f"{skill}.json"
        s = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else \
            {"skill": skill, "accept": [skill], "cases": []}
        ids = {c["id"] for c in s["cases"]}
        n = 0
        for r in (r for r in take if r["skill"] == skill):
            cid = f"{skill}-mined-{r['id']}"
            if cid in ids:
                continue
            s["cases"].append({"id": cid, "prompt": case_prompt(r), "expect": r["expect"],
                               "kind": f"mined-{r['signal']}", "source": "mined"})
            ids.add(cid)
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        added[skill] = n
    return added


def is_inside(p: Path, root: Path) -> bool:
    p, root = Path(p).expanduser().resolve(), Path(root).resolve()
    return p == root or root in p.parents
