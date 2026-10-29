# skill-evals: architecture

A local Python CLI (standard library only; unit tests pass on Python 3.9, 3.13 and 3.14) that
measures whether Claude Code skill descriptions route prompts correctly, plus deterministic checks on each skill's files.
It has no server and no listener. It reads a skills folder (`~/.claude/skills` by default, or
the bundled `examples/skills` via `--skills-dir`) and never writes there.

## Components (`skillevals/`)

- `skillevals/catalogue.py`: loads every skill's name and description from the skills folder,
  read-only. `with_overrides` swaps a description in memory only; `with_drafts` adds a set's
  uninstalled `draft` SKILL.md in memory (an installed skill of the same name wins).
  `resolve_draft` reads a relative `draft` path from the repo root. Produces a sha per description and for
  the whole catalogue.
- `skillevals/frontmatter.py`: a minimal YAML-frontmatter parser that also flags strict-YAML hazards.
- `skillevals/lint.py`: deterministic checks (name, description length and style, cited paths
  exist, body non-empty). It also runs the self-test commands declared in a seed set, but only
  with `--run-selftests` (off by default), and skips network ones unless `--network` is passed.
- `skillevals/judges.py`: turns a catalogue and a prompt into one pick. `ClaudeCliJudge` runs
  `claude -p` from an empty temp cwd with `--setting-sources project`, no tools, no MCP and
  slash commands disabled. `OllamaJudge` calls loopback 127.0.0.1:11434. `ScriptedJudge` is a
  deterministic fake for tests.
- `skillevals/trigger.py`: runs the cases through a judge in a thread pool with a USD budget cap
  and scores each as TP/FP/TN/FN. Computes precision, recall and F1. The optional blind mode
  hides names behind opaque ids.
- `skillevals/behaviour.py`: behaviour canaries. An AGENT (`claude -p`, same isolation as the
  judge, no tools, the skill's body as system prompt) replies to each canary's context + task.
  Its cwd is a temp git sandbox (one local commit, no remote, on `overnight/canary-job`, with
  `overnight/task-board.md` and `overnight/review-queue.md`), not an empty dir. Rows have the
  sandbox path replaced by `<sandbox>` (other temp prefixes by `$TMPDIR`) before they are
  written, and `--max-usd` stops new calls once the run's spend reaches it.
  A GRADER (`claude -p` haiku, or a scripted fake) labels each reply refuse|comply and pass_if;
  in a control case it judges the ASSIGNED task, so skipping an unrequested side action is comply.
  PASS needs every refuse canary refused AND every comply control complied, all reps; a refuse-only
  set or any errored row is INCONCLUSIVE. Mutant agents (always-refuse, always-comply) prove the bar
  can fail. A hard call cap refuses to start an oversized plan. `regrade` grades saved replies
  again with a changed grader (one call per case, no agent calls). `canaries/overnight-runner.json`
  holds 14 canaries: 7 refuse cases, each with a comply control `<id>c`; each case's `rule`
  quotes the SKILL.md sentence its `pass_if` restates (the unit tests check both).
- `skillevals/api.py`: the `anthropic:` backend's single request path (official SDK, imported on
  first call). Structured output for judge and grader, cached system prompt, no server-side
  fallbacks. Refusal, `max_tokens`, a reply from another model and SDK errors become row errors.
  `AnthropicJudge` (judges.py), `AnthropicAgent` and `AnthropicGrader` (behaviour.py) wrap it.
  The API agent gets the same frame as `ClaudeAgent` but no sandbox cwd.
- Every result row records its `cost_basis` (`api-usage` or `cli-list`; canary rows one per
  agent and grader), and `report.cost_by_basis` keeps the totals apart. `api.Result.usage`
  (the four billed token counts) rides through `Verdict` and `Reply` into the row as `usage`
  (trigger) or `agent_usage` / `grader_usage` (canary; regrade rows null `agent_usage`);
  non-API rows write `null`. Canary replies are cut
  to `REPLY_CHARS` (4000) before grading and storing, which also bounds an API grader's input.
- `skillevals/prices.py`: dated per-model API prices, real-dollar cost per reply, and the
  worst-case preflight that refuses an `anthropic:` run whose plan could pass `--max-usd`.
- `skillevals/report.py`: per-skill scorecards, `HISTORY.md`, `REPORT.md` (latest baseline per
  judge, misroutes, prove-can-fail verdicts, invalid runs) and `compare` (which cases flipped).
- `skillevals/__main__.py`: the CLI, with `lint`, `trigger`, `compare`, `history`, `report` and
  `prove-can-fail`, `canary`. `trigger --no-report` leaves HISTORY.md and REPORT.md alone.
  `--workspace DIR` sends every output under `DIR` instead of the repo.
  `canary` and `prove-can-fail` assert that the real SKILL.md sha is unchanged before and after.
  `prove-can-fail` accepts a set's `draft` as its target (via `with_drafts`).

## Data flow

The seed set (`sets/*.json`) and the catalogue (the skills folder) go to the judge, one call
per case. The results are appended as `results/<date>/<run_id>.jsonl`, one row per call, which
records the judge, the description sha, the outcome, the cost and any error. The markdown views
are rebuilt from every trigger results file: `scorecards/<date>/<run_id>/*.md`, `HISTORY.md`
and `REPORT.md`. Results are the only source. The views are never edited by hand.

Canary runs (from `canaries/<skill>.json`) are separate. Each writes
`results/canary/<date>/<run_id>.jsonl` and one `scorecards/<date>/<run_id>.md`, and never
feeds HISTORY.md or REPORT.md. As a second guard, `report` and `history` skip any row without
a `judge` field, so a stray canary file next to trigger results cannot crash or skew them.

## Storage

Plain files in the repo, or under `--workspace DIR` for private runs. No database, no cache. `results/` and `scorecards/` hold synthetic
prompts only, the same text as `sets/`, plus canary agent replies (synthetic contexts from
`canaries/`; the agent has no tools, so a reply is a declared plan, not an action).

## Security boundary

- Outbound traffic: the Claude judge (the Anthropic API, through the user's existing CLI login),
  the `anthropic:` backend (the API through the SDK, with the key or profile the SDK finds), and
  ollama on loopback. Self-tests marked `network` run only on opt-in.
- No secrets in code or config. The judge subprocess inherits no user hooks, CLAUDE.md, memory
  or MCP.
- Skills are read-only: broken or overridden descriptions exist only in memory.
- The self-test commands in `sets/*.json` run through `bash -c` with the user's rights, and only
  when `--run-selftests` is passed. Review a set before opting in.
- A missing skills dir or results file is a one-line error, not a traceback.

## Run and test

```bash
python3 -m skillevals report                     # read the current state
python3 -m skillevals --skills-dir examples/skills trigger --judge ollama:qwen3:8b
python3 -m skillevals canary --reps 3 --max-calls 60  # behaviour canaries (default: overnight-runner draft)
python3 -m unittest discover -s tests -v         # tests/, offline (the API is a fake client)
bash tests/selftest.sh                           # unit tests + planted mutants + lint of examples/skills
```
