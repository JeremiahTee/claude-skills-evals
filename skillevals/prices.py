"""API prices in USD per million tokens, and the real-dollar spend cap for anthropic: runs.

Source: https://platform.claude.com/docs/en/about-claude/pricing, read on PRICES_AS_OF.
Standard (global) routing, 5-minute cache writes, no batch discount. Re-read the page before a
paid run if this date is old: a wrong table makes the cap wrong.
"""
from __future__ import annotations

import math

PRICES_AS_OF = "2026-10-28"

# model id: (input, output, 5-minute cache write, cache read)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
}
# Haiku 5.5 charges more for prompts over 100,000 input tokens (cached tokens count).
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.625, 0.05))}


def _rates(model: str, prompt_tokens: int) -> tuple[float, float, float, float]:
    if model not in PRICES:
        raise SystemExit(f"no price for {model!r} in skillevals/prices.py (as of {PRICES_AS_OF}); "
                         "add it before running under a spend cap")
    if model in LONG_PROMPT and prompt_tokens > LONG_PROMPT[model][0]:
        return LONG_PROMPT[model][1]
    return PRICES[model]


def cost(model: str, usage: dict) -> float:
    """Real dollars for one response's usage (thinking is billed inside output_tokens)."""
    inp, cw, cr = (int(usage.get(k) or 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = int(usage.get("output_tokens") or 0)
    r_in, r_out, r_cw, r_cr = _rates(model, inp + cw + cr)
    return (inp * r_in + out * r_out + cw * r_cw + cr * r_cr) / 1e6


def estimate_tokens(text: str) -> int:
    """A deliberately loose upper bound for when no count is available: 2 UTF-8 bytes per token,
    so non-Latin text and emoji (several bytes per character) are not under-counted."""
    return math.ceil(len(text.encode("utf-8")) / 2)


def worst_case(model: str, calls: int, input_tokens: int, max_tokens: int) -> float:
    """Every call pays its whole input at the cache-write rate (the highest input rate) and
    writes max_tokens of output."""
    _, r_out, r_cw, _ = _rates(model, input_tokens)
    return calls * (input_tokens * r_cw + max_tokens * r_out) / 1e6


def preflight(plan: list[tuple[str, int, int, int]], max_usd: float) -> float:
    """plan: (model, calls, input_tokens per call, max_tokens per call) for every planned call,
    agent and grader alike. Refuses to start when the worst case could pass the cap."""
    worst = sum(worst_case(m, n, i, o) for m, n, i, o in plan)
    if worst > max_usd:
        raise SystemExit(f"worst case ${worst:.4f} > --max-usd ${max_usd}: refusing to start "
                         f"(prices as of {PRICES_AS_OF}). Raise the cap or shrink the plan.")
    return worst
