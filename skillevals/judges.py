"""Judges: something that reads (catalogue, user prompt) and names the skill it would pick.

Three judges, and every result row records which one produced it:

- ClaudeCliJudge  `claude -p` in print mode, fully isolated (no hooks, tools, skills, MCP,
                   CLAUDE.md-from-cwd). This is the real model family doing the routing, but
                   through a simulated router prompt, so it is still a PROXY for Claude Code's
                   internal skill selection. Label: "claude-cli:<model>".
- OllamaJudge     a local model (qwen3:8b) at 127.0.0.1:11434. Free and private, but a much
                   weaker, different model. Label: "ollama:<model> (PROXY)".
- AnthropicJudge  the API through the official SDK (skillevals/api.py), billed in real dollars.
                   Same router prompt, so still a PROXY. Label: "anthropic:<model-id>@<effort>".
- ScriptedJudge   a deterministic fake, used by the unit tests to prove the metrics and the
                   regression logic without calling any model.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass

ROUTER_SYSTEM = """You are the skill router inside Claude Code, an AI coding assistant.
Before answering a user message, you decide whether ONE of the available skills should be
invoked. A skill is invoked only when the message matches what its description says it is for.
Respect every "Do NOT use" clause: if a description excludes the case, do not pick that skill.
If no skill fits, answer none.

Available skills:
{catalogue}

Reply with a single JSON object and nothing else: {{"skill": "<skill name or none>"}}"""


@dataclass
class Verdict:
    picked: str | None          # skill name, "none", or None if unparseable
    raw: str
    latency_s: float
    cost_usd: float = 0.0
    error: str | None = None


def parse_pick(text: str, names: set[str]) -> str | None:
    m = re.search(r"\{[^{}]*\}", text or "")
    if m:
        try:
            v = str(json.loads(m.group(0)).get("skill", "")).strip().lstrip("/")
            if v.lower() == "none":
                return "none"
            if v in names:
                return v
        except (json.JSONDecodeError, AttributeError):
            pass
    return None


class ClaudeCliJudge:
    def __init__(self, model: str = "haiku", per_call_usd: float = 0.05, timeout: int = 180):
        self.model, self.per_call_usd, self.timeout = model, per_call_usd, timeout
        self.label = f"claude-cli:{model}"
        self.workers = 4

    def __call__(self, system: str, prompt: str, names: set[str]) -> Verdict:
        cwd = tempfile.mkdtemp(prefix="skill-evals-judge-")  # empty dir: no project CLAUDE.md
        cmd = [
            "claude", "-p", prompt,
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "",                       # no tools at all
            "--disable-slash-commands",          # hides the user's real skills from the judge
            "--strict-mcp-config",               # no MCP servers
            "--setting-sources", "project",      # skip user settings => none of the user's hooks fire
            "--no-session-persistence",
            "--max-budget-usd", str(self.per_call_usd),
            "--output-format", "json",
        ]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return Verdict(None, "", time.time() - t0, 0.0, "timeout")
        dt = time.time() - t0
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            return Verdict(None, p.stdout[-300:] + p.stderr[-300:], dt, 0.0,
                           f"non-json output (exit {p.returncode})")
        text = d.get("result") or ""
        cost = float(d.get("total_cost_usd") or 0.0)
        if d.get("is_error"):
            # subtype can read "success" even when the CLI refused (e.g. a usage limit), so the
            # first line of the reply is what makes the error readable.
            first = (text.strip().splitlines() or [""])[0][:120]
            return Verdict(None, text, dt, cost, f"cli error ({d.get('subtype')}): {first}")
        pick = parse_pick(text, names)
        return Verdict(pick, text, dt, cost, None if pick else "unparseable reply")


PICK_SCHEMA = {"type": "object", "properties": {"skill": {"type": "string"}},
               "required": ["skill"], "additionalProperties": False}


class AnthropicJudge:
    def __init__(self, model: str, effort: str | None = None, client=None, max_tokens: int = 4000):
        from . import api
        self.call = api.Caller(model, effort, max_tokens, client)
        self.label, self.model, self.max_tokens = self.call.label, model, max_tokens
        self.workers = 4

    def __call__(self, system: str, prompt: str, names: set[str]) -> Verdict:
        r = self.call(system, prompt, PICK_SCHEMA)
        if r.error:
            return Verdict(None, r.text, r.latency_s, r.cost_usd, r.error)
        pick = parse_pick(r.text, names)
        return Verdict(pick, r.text, r.latency_s, r.cost_usd, None if pick else "unparseable reply")


class OllamaJudge:
    def __init__(self, model: str = "qwen3:8b", host: str = "http://127.0.0.1:11434",
                 timeout: int = 600):
        self.model, self.host, self.timeout = model, host, timeout
        self.label = f"ollama:{model} (PROXY)"
        self.workers = 1  # one local GPU/CPU

    def __call__(self, system: str, prompt: str, names: set[str]) -> Verdict:
        body = json.dumps({
            "model": self.model, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0, "num_ctx": 16384}, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read())
        except Exception as e:  # noqa: BLE001 - report any transport failure as a row error
            return Verdict(None, "", time.time() - t0, 0.0, f"ollama: {e}")
        text = d.get("message", {}).get("content", "")
        pick = parse_pick(text, names)
        return Verdict(pick, text, time.time() - t0, 0.0, None if pick else "unparseable reply")


class ScriptedJudge:
    """Deterministic fake. Picks the skill whose description shares the most words with the
    prompt (a crude keyword router). Good enough to show that a broken description loses
    its prompts, which is all the unit tests need."""

    label = "scripted:keyword-overlap (TEST ONLY)"
    workers = 1

    def __call__(self, system: str, prompt: str, names: set[str]) -> Verdict:
        words = set(re.findall(r"[a-z0-9+]{3,}", prompt.lower()))
        best, score = "none", 0
        for line in system.splitlines():
            m = re.match(r"- ([a-z0-9-]+): (.*)", line)
            if not m:
                continue
            pos = m.group(2).lower().split("do not use")[0]
            s = len(words & set(re.findall(r"[a-z0-9+]{3,}", pos)))
            if s > score:
                best, score = m.group(1), s
        return Verdict(best if score >= 2 else "none", best, 0.0)


def make(spec: str):
    kind, _, model = spec.partition(":")
    if kind == "claude":
        return ClaudeCliJudge(model or "haiku")
    if kind == "ollama":
        return OllamaJudge(model or "qwen3:8b")
    if kind == "scripted":
        return ScriptedJudge()
    if kind == "anthropic":
        from . import api
        return AnthropicJudge(*api.parse_spec(model or "claude-haiku-5-5"))
    raise ValueError(f"unknown judge {spec!r} (use claude[:model], anthropic:<model-id>[@effort], "
                     "ollama[:model], scripted)")
