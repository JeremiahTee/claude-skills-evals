"""The anthropic: backend's one request path, shared by the judge, the canary agent and the grader.

Uses the official `anthropic` SDK, imported only on the first real call (`pip install anthropic`);
the rest of the harness stays standard library only. Credentials are whatever the SDK resolves
(ANTHROPIC_API_KEY or a stored profile); this module never reads, logs or stores a key.

Every failure becomes a row error, never a verdict: a refusal, a truncated reply, a reply from a
model other than the one requested, or an SDK exception (the SDK has already retried rate limits
and server errors by then). Server-side fallbacks are not requested: they would let another model
answer under this model's label.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import prices

EFFORTS = ("low", "medium", "high", "xhigh", "max")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass
class Result:
    text: str
    latency_s: float
    cost_usd: float = 0.0
    error: str | None = None
    usage: dict | None = None  # the four billed token counts (USAGE_KEYS); None if no response came back


class Caller:
    cost_basis = "api-usage"

    def __init__(self, model: str, effort: str | None = None, max_tokens: int = 4000, client=None):
        prices._rates(model, 0)  # an unpriced model cannot run under a real-dollar cap
        if effort is not None and effort not in EFFORTS:
            raise SystemExit(f"effort (the @suffix) must be one of {', '.join(EFFORTS)}")
        self.model, self.effort, self.max_tokens, self._client = model, effort, max_tokens, client
        self.label = f"anthropic:{model}@{effort or 'default'}"

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise SystemExit("the anthropic: backend needs the SDK: pip install anthropic") from None
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, system: str, prompt: str, schema: dict | None = None) -> Result:
        out_cfg = {}
        if self.effort:
            out_cfg["effort"] = self.effort
        if schema:
            out_cfg["format"] = {"type": "json_schema", "schema": schema}
        req = {"model": self.model, "max_tokens": self.max_tokens,
               # the system prompt repeats across a run, so it is cached
               "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
               "messages": [{"role": "user", "content": prompt}]}
        if out_cfg:
            req["output_config"] = out_cfg
        t0 = time.time()
        try:
            r = self.client().messages.create(**req)
        except (SystemExit, AssertionError):  # AssertionError: the tests' no-real-call guard
            raise
        except Exception as e:  # noqa: BLE001 - any SDK failure is reported as a row error
            status = getattr(e, "status_code", None)
            return Result("", time.time() - t0, 0.0, f"api {type(e).__name__}" + (f" {status}" if status else ""))
        dt = time.time() - t0
        usage = {k: getattr(r.usage, k, 0) or 0 for k in USAGE_KEYS}
        cost = prices.cost(self.model, usage)  # billed even when the reply is unusable
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        if r.model != self.model:
            return Result(text, dt, cost, f"model mismatch: asked {self.model}, got {r.model}", usage)
        if r.stop_reason == "refusal":
            cat = getattr(r.stop_details, "category", None) if r.stop_details else None
            return Result(text, dt, cost, f"refusal:{cat or 'unspecified'}", usage)
        if r.stop_reason == "max_tokens":
            return Result(text, dt, cost, "max_tokens", usage)
        if r.stop_reason != "end_turn":  # pause_turn, a context-window stop, anything new: not a full reply
            return Result(text, dt, cost, f"stop:{r.stop_reason}", usage)
        return Result(text, dt, cost, usage=usage)


def parse_spec(spec: str) -> tuple[str, str | None]:
    """'claude-opus-5-5@low' -> ('claude-opus-5-5', 'low')."""
    model, _, effort = spec.partition("@")
    return model, effort or None
