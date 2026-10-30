# skill-evals

A small harness that answers one question about a Claude Code skill: **does its description
make it fire when it should, and stay quiet when it should not?** On top of that, it runs
deterministic checks on each skill's files, and behaviour canaries for a skill that runs
unattended. It reads a skills folder (`~/.claude/skills` by default, or the bundled example
catalogue in `examples/skills` with `--skills-dir examples/skills`) and never writes there.

Standard library only, except the optional `anthropic:` backend (`pip install anthropic`). Unit tests and `tests/selftest.sh` pass on Python 3.9, 3.13 and 3.14.

## Results (2026-10-23, bundled example catalogue)

All numbers below come from runs on 2026-10-23, after the grader fix (PR #1). The trigger
baselines and the canary runs were each run twice, because one run of a model eval is a
sample, not a score; prove-can-fail ran once. Trigger tables are in `REPORT.md` (latest run per
judge) and `scorecards/`; canary tables are in `scorecards/2026-10-23/`; every row is in
`results/`.

**Trigger evals** (`claude:haiku` judge, 34 prompts: 19 should trigger, 15 near misses, one
sample per prompt per run). *Blind* means the judge sees opaque ids instead of skill names, so
only the descriptions can route.

| names | run | precision | recall | F1 | misroutes |
|---|---|---|---|---|---|
| real | 1 | 0.95 | 1.00 | 0.97 | 1 |
| real | 2 | 1.00 | 1.00 | 1.00 | 0 |
| blind | 1 | 1.00 | 1.00 | 1.00 | 0 |
| blind | 2 | 1.00 | 1.00 | 1.00 | 0 |

The one misroute (`gen-commits-07`, "just commit the README typo fix", picked although the
description excludes a single obvious commit) appeared in one of two runs, and also on
2026-10-19. Across all six full haiku runs so far, three prompts (`gen-commits-01`,
`gen-commits-07`, `handoff-06`) flip between runs. With 34 prompts and one sample each, these
scores cannot separate two good descriptions; they can catch a broken one.

**Prove-can-fail** (`claude:haiku`, blind names, `gen-commits`): F1 1.00 at baseline, 0.00 with
the description replaced by a wrong one, 1.00 once restored. PASS: the harness can see a bad
description.

**Behaviour canaries** (`claude:sonnet` agent, `claude:haiku` grader, 14 cases x 3 reps = 42
rows per run). A run counts as certified only when two independent gradings of the same
replies agree.

| run | first grading | second grading | verdict |
|---|---|---|---|
| fresh run 1 | PASS, 42/42 | PASS, 42/42 | certified PASS |
| fresh run 2 | FAIL, 38/42 (`can-04` x3, `can-05` rep 0) | 39/42 pass, 3 rows unparseable (`can-05`); a third grading of `can-05` alone passes 3/3 | not certified: the gradings disagree |
| 2026-10-19 replies, regraded | (old grader) 41/42 | PASS, 42/42 | the old `can-04c` failure was the grader |

What the FAILs mean: in fresh run 2, the four failing replies all decline the injected ask in
plain words (for example "I won't push and won't edit settings or the allow list"). The same
grader, given the same replies again, passed `can-04` on all three reps. These are grader
failures, not agent failures, and the grader (haiku, one call per case) is the noisiest part
of this harness: on identical input it flipped a verdict and once returned an unreadable reply.
Run 2 stays not certified: the partial third grading of `can-05` does not change that.

Limits worth knowing: the agent has no tools, so a canary checks what it says it would do, not
what it does. The refuse cases test injection vectors that the skill under test names
explicitly, so they show the agent following its rules under pressure, not resistance to
novel attacks. Every judge is a proxy for Claude Code's own router.

- The `always-refuse` mutant agent (scripted grader, 2026-10-19) FAILs, as it must: an agent
  that refuses everything fails the comply controls.
- `ollama:qwen3:8b` was not rerun; its last result (2026-10-19, F1 0.95) is in `REPORT.md`.
- Cost at CLI list price, 2026-10-23: $0.025 for 166 trigger judge calls, $0.65 for 155 canary
  calls (two fresh runs plus four regrades). The Claude judges run on the user's plan.

## Quickstart

```bash
git clone https://github.com/JeremiahTee/claude-skills-evals.git && cd claude-skills-evals
bash tests/selftest.sh                                    # offline: unit tests + mutants + lint
python3 -m skillevals --skills-dir examples/skills lint   # deterministic checks on the examples
```

## What it measures

1. **Trigger evals** (`trigger`). Each seed set in `sets/<skill>.json` holds labelled user
   prompts: `trigger` (the skill should be picked) or `no` (a near miss that belongs to another
   skill, or to none). A *judge* model sees the whole catalogue of skills (name +
   description), the way the real router does, and names one skill or `none`. Outcomes are
   TP/FP/TN/FN, from which it computes precision, recall and F1 per skill.
2. **Deterministic checks** (`lint`). Frontmatter parses; the name is valid; the description is
   ≤1024 chars and has a "Use when" and a "Do NOT use"; the paths the skill cites exist; there
   are no unquoted `: ` YAML hazards. A set may also declare self-test commands; they run only
   with `--run-selftests` (see Security).
3. **Blind names** (`--blind-names`). This swaps skill names for opaque ids (`skill-01`, ...),
   so only the descriptions can route. Without it, a prompt that says "commit" finds `gen-commits`
   even when its description is broken, which is why the named prove-can-fail below is weak.
4. **Prove-can-fail** (`prove-can-fail <skill>`). It replaces one description in memory with a
   wrong one, checks that the score drops, restores it and checks that the score recovers.
   It also checks that `SKILL.md`'s sha is unchanged. A harness that cannot fail is not a harness.
5. **Behaviour canaries** (`canary`). An agent runs under a skill's body and reads a context
   (a peer message, a planted note, a README) carrying an injected ask. It must refuse those and
   still do the paired legitimate controls. `canaries/overnight-runner.json` tests the bundled
   example draft `examples/skills/overnight-runner/SKILL.md` with 14 canaries: 7 refuse cases,
   each paired with a comply control (`can-01` and `can-01c`, ...). Each case's `rule` quotes
   the SKILL.md sentence its `pass_if` restates. Mutant agents (`always-refuse`,
   `always-comply`) show the pass bar can fail.

## Judges (every result row records which one)

| judge | what it is | cost |
|---|---|---|
| `claude:haiku` | `claude -p`, isolated: empty temp cwd, `--setting-sources project` (none of the user hooks fire), no tools, no MCP, slash commands disabled | uses the plan; CLI reports a list-price figure |
| `anthropic:<model-id>[@effort]` | the Claude API through the official SDK, same router prompt, structured output; e.g. `anthropic:claude-haiku-5-5@low` | real dollars from the reply's token usage and `skillevals/prices.py` |
| `ollama:qwen3:8b` | local model at 127.0.0.1:11434 | free, private, much weaker |
| `scripted` | keyword-overlap fake, unit tests only | none |

Every judge is a **proxy** for Claude Code's internal skill selection: it reads the real
descriptions, but through a simulated router prompt (`judges.ROUTER_SYSTEM`). Compare scores
only within one judge.

## Commands

`--skills-dir` comes before the subcommand. The examples use the bundled catalogue; drop the
flag to evaluate your own `~/.claude/skills`.

```bash
python3 -m skillevals --skills-dir examples/skills lint     # deterministic checks, 3 seeded skills
python3 -m skillevals --skills-dir examples/skills lint --run-selftests  # also run the sets' self-test commands
python3 -m skillevals --skills-dir examples/skills trigger --judge ollama:qwen3:8b  # full run, local
python3 -m skillevals --skills-dir examples/skills trigger --judge claude:haiku --max-usd 0.5
python3 -m skillevals --skills-dir examples/skills trigger --judge claude:haiku --skill gen-commits --blind-names
python3 -m skillevals --skills-dir examples/skills trigger --override gen-commits=draft.txt  # new description, in memory
python3 -m skillevals --skills-dir examples/skills prove-can-fail gen-commits --judge claude:haiku --blind-names
python3 -m skillevals canary --reps 3 --max-calls 60         # behaviour canaries, example draft
python3 -m skillevals canary --agent mutant:always-refuse --grader scripted  # must FAIL
python3 -m skillevals --skills-dir examples/skills trigger --judge anthropic:claude-haiku-5-5@low --max-usd 0.5
python3 -m skillevals canary --agent anthropic:claude-sonnet-5-5@low --grader anthropic:claude-fable-5-1@low --max-usd 2
python3 -m skillevals --workspace ~/evals-private trigger --judge scripted  # results go to the workspace, not the repo
python3 -m skillevals compare results/A.jsonl results/B.jsonl  # exit 1 on any F1 regression
python3 -m skillevals report                                 # rebuild REPORT.md (trigger runs only)
python3 -m skillevals history                                # rebuild HISTORY.md
bash tests/selftest.sh                                       # offline: unit tests + mutants + lint
```

`prove-can-fail` also works on a set's `draft` (a SKILL.md not installed yet): the draft joins
the catalogue in memory, and its file's sha is checked before and after.

The `anthropic:` backend is tested offline only (a fake client); no paid run has been made
with it yet. Before an `anthropic:` run starts, it prices the worst case (every planned call at
the cache-write input rate plus its full `max_tokens` of output) and refuses to start if that
is over `--max-usd`. A refusal, a truncated reply, a reply from another model or an SDK error
is a row error, never a verdict. Server-side model fallbacks are not used.

`--workspace DIR` (before the subcommand) writes results, scorecards, `HISTORY.md` and
`REPORT.md` under `DIR` instead of the repo. Use it for private skills and cases.

## Outputs

- `results/<date>/<run>.jsonl`: one row per judge call (prompt, expected result, pick, outcome,
  judge, description sha, catalogue sha, cost, latency, error).
- `results/canary/<date>/<run>.jsonl` and `scorecards/<date>/<run>.md`: one canary run (one row
  per agent reply). Canary runs stay out of `HISTORY.md` and `REPORT.md`. The agent's temp
  sandbox path is written as `<sandbox>`, never as the machine's absolute path. Each reply is
  cut to 4000 characters before it is graded and stored, so a regrade reads exactly what the
  first grading read. A `--regrade` file is named after the source run's agent (`--agent` is
  ignored), refuses a results file that mixes agents, and skips a case whose saved replies all
  errored (no grader call, no row).
- Costs are kept per basis: `real dollars (API)` for `anthropic:` rows, `list price (CLI)` for
  `claude -p` rows. Reports never add the two.
- `anthropic:` rows also keep the token counts the call was billed for (`input_tokens`,
  `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`): `usage` on
  trigger rows, `agent_usage` and `grader_usage` on canary rows (the grader's on the first row
  of each case, like its cost: rep 0 in a run, the first re-graded reply in a `--regrade`).
  A refused or truncated reply keeps its usage, since it was billed. Regrade rows write
  `agent_usage: null` (no agent call was made). Other backends write `null`.
- `scorecards/<date>/<run>/`: one markdown scorecard per skill, plus `_summary.md`.
- `REPORT.md`: the file to read. It shows the latest full baseline per judge, the misroutes,
  the prove-can-fail verdicts and the invalid runs. A run where more than 20% of rows errored
  is not a measurement.
- `HISTORY.md`: every run, one row per skill. A changed `desc sha` means the description changed.

The scorecards, `REPORT.md` and `HISTORY.md` are regenerated from `results/`. Do not edit them by hand.

## Testing the tests

`tests/selftest.sh` runs the unit tests. It then plants 28 known bugs (mutants) in a temp
copy and requires the unit tests to catch each one. Finally it lints the bundled example
skills (`examples/skills`) read-only.
If a mutant survives, the check it targets cannot fail.

## Security and privacy

- No listener, no server. The only outbound traffic is the judge, agent or grader call
  (`claude -p` or the API to Anthropic, or ollama on loopback) and any self-test marked `network` (off by default).
- Nothing from a set's JSON is executed by default. A set's `selftests` are shell commands
  (`bash -c`, your user's rights, cwd `~` unless the set gives one). They run only when you pass
  `--run-selftests` to `lint` or `trigger`; otherwise lint reports them as SKIP. Read a set
  before you opt in.
- No secrets in code or config. The Claude judge runs with the user's existing CLI login;
  the `anthropic:` backend uses whatever key or profile the SDK finds and never logs or stores it.
- Judge calls are isolated from user settings, so no personal hooks run and no personal
  context (CLAUDE.md, memory, MCP) reaches the judge. The eval prompts are synthetic.
- Skills are read-only: overrides and "broken" descriptions exist only in memory.
  `prove-can-fail` asserts the file's sha before and after.

## Seeded skills

`examples/skills` is a small invented catalogue (10 skills: `gen-commits`, `eli5`, `handoff`,
`resume`, `wrap`, `code-review`, `journal`, `extract-learnings`, `notes-search` and the
`overnight-runner` draft), so anyone can reproduce a run. Three have trigger sets:
`gen-commits`, `eli5` and `handoff`, 34 prompts, 19 positive and 15 negative. The draft also
has the 14 behaviour canaries in `canaries/overnight-runner.json`. The rest are
there as near-miss targets. To add a set, create `sets/<skill>.json` in the same shape.
`SeedSets` in the unit tests validates it.

`results/` and `scorecards/` hold the 2026-10-19 and 2026-10-23 runs; Results summarises the 2026-10-23 ones. Your runs add new files beside them.

## License

MIT. See `LICENSE`.
