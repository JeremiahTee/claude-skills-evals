# skill-evals report (regenerated from results/*/*.jsonl — do not hand-edit)

6 run(s), 132 judge call(s), reported judge cost $0.0170 (CLI list-price basis; ollama is local and reports 0).

Every judge is a PROXY for Claude Code's own skill router: it reads the same names and
descriptions through a simulated router prompt. Compare scores only within one judge.

## Latest full baseline per judge

### claude-cli:haiku — `2026-10-19T233809-claude-cli-haiku-baseline`

| skill | precision | recall | F1 | TP | FP | TN | FN | errors |
|---|---|---|---|---|---|---|---|---|
| eli5 | 1.00 | 1.00 | 1.00 | 6 | 0 | 6 | 0 | 0 |
| gen-commits | 0.83 | 0.83 | 0.83 | 5 | 1 | 3 | 1 | 0 |
| handoff | 1.00 | 1.00 | 1.00 | 7 | 0 | 5 | 0 | 0 |
| **all** | 0.95 | 0.95 | 0.95 | 18 | 1 | 14 | 1 | 0 |

Misroutes:

- `gen-commits-01` FN → `none`: "/gen-commits"
- `gen-commits-07` FP → `gen-commits`: "just commit the README typo fix"

### claude-cli:haiku · blind names — `2026-10-19T233832-claude-cli-haiku-baseline-blind`

| skill | precision | recall | F1 | TP | FP | TN | FN | errors |
|---|---|---|---|---|---|---|---|---|
| eli5 | 1.00 | 1.00 | 1.00 | 6 | 0 | 6 | 0 | 0 |
| gen-commits | 1.00 | 1.00 | 1.00 | 6 | 0 | 4 | 0 | 0 |
| handoff | 1.00 | 0.86 | 0.92 | 6 | 0 | 5 | 1 | 0 |
| **all** | 1.00 | 0.95 | 0.97 | 18 | 0 | 15 | 1 | 0 |

Misroutes:

- `handoff-06` FN → `none`: "/handoff"

### ollama:qwen3:8b (PROXY) — `2026-10-19T233644-ollama-qwen3-8b-baseline`

| skill | precision | recall | F1 | TP | FP | TN | FN | errors |
|---|---|---|---|---|---|---|---|---|
| eli5 | 1.00 | 1.00 | 1.00 | 6 | 0 | 6 | 0 | 0 |
| gen-commits | 0.75 | 1.00 | 0.86 | 6 | 2 | 2 | 0 | 0 |
| handoff | 1.00 | 1.00 | 1.00 | 7 | 0 | 5 | 0 | 0 |
| **all** | 0.90 | 1.00 | 0.95 | 19 | 2 | 13 | 0 | 0 |

Misroutes:

- `gen-commits-07` FP → `gen-commits`: "just commit the README typo fix"
- `gen-commits-08` FP → `gen-commits`: "fix the message on my last commit, it has a typo"

## Prove-can-fail (break a description in memory, restore it)

| judge | names | skill(s) | F1 baseline | F1 broken | F1 restored | verdict |
|---|---|---|---|---|---|---|
| claude-cli:haiku | blind | gen-commits | 1.00 | 0.00 | 0.91 | PASS |

## All runs

| run | judge | variant | skills | rows | F1 | errors | cost USD |
|---|---|---|---|---|---|---|---|
| `2026-10-19T233644-ollama-qwen3-8b-baseline` | ollama:qwen3:8b (PROXY) | baseline | 3 | 34 | 0.95 | 0 | 0.0000 |
| `2026-10-19T233809-claude-cli-haiku-baseline` | claude-cli:haiku | baseline | 3 | 34 | 0.95 | 0 | 0.0059 |
| `2026-10-19T233832-claude-cli-haiku-baseline-blind` | claude-cli:haiku | baseline-blind | 3 | 34 | 0.97 | 0 | 0.0062 |
| `2026-10-19T233855-claude-cli-haiku-baseline-blind` | claude-cli:haiku | baseline-blind | 1 | 10 | 1.00 | 0 | 0.0013 |
| `2026-10-19T233902-claude-cli-haiku-broken-wrong-blind` | claude-cli:haiku | broken-wrong-blind | 1 | 10 | 0.00 | 0 | 0.0023 |
| `2026-10-19T233908-claude-cli-haiku-restored-blind` | claude-cli:haiku | restored-blind | 1 | 10 | 0.91 | 0 | 0.0013 |
