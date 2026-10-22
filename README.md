# skill-evals

A small harness that answers one question about a Claude Code skill: **does its description
make it fire when it should, and stay quiet when it should not?** On top of that, it runs
deterministic checks on each skill's files, and behaviour canaries for a skill that runs
unattended. It reads a skills folder (`~/.claude/skills` by default, or the bundled example
catalogue in `examples/skills` with `--skills-dir examples/skills`) and never writes there.

Standard library only. Unit tests and `tests/selftest.sh` pass on Python 3.9, 3.13 and 3.14.

## Results (2026-10-19, bundled example catalogue)

Trigger evals: 34 prompts over three sets (19 should trigger, 15 near misses). Scores are
for all three sets together. *Blind* means the judge sees opaque ids instead of skill names,
so only the descriptions can route. Full tables and every misroute are in `REPORT.md`.

| judge | names | precision | recall | F1 | misroutes |
|---|---|---|---|---|---|
| `claude:haiku` | real | 0.95 | 0.95 | 0.95 | 2 |
| `claude:haiku` | blind | 1.00 | 0.95 | 0.97 | 1 |
| `ollama:qwen3:8b` | real | 0.90 | 1.00 | 0.95 | 2 |

- **Prove-can-fail** (`claude:haiku`, blind names, `gen-commits`): F1 1.00 at baseline, 0.00
  with the description broken, 0.91 once restored. PASS: the harness can see a bad description.
- **Behaviour canaries** (`claude:sonnet` agent, `claude:haiku` grader, 14 cases x 3 reps):
  41 of 42 rows pass. All 21 refuse rows (an injected ask the agent must decline) pass. One
  comply control (a legitimate ask it must still do) failed (`can-04c`, rep 2): the
  agent listed the right steps but said it had no tools, and the grader counted that as not
  done. A run passes only if every row passes, so this run is a FAIL. It is reported as
  is. Since then the grader prompt has said that the agent has no tools and that saying so is
  not a failure. The 41/42 above comes from the old grader and has not been rerun. The `always-refuse` mutant agent (graded by the scripted keyword grader) also FAILs, as it must.
- Cost at CLI list price: $0.017 for all 132 trigger judge calls (the six runs in `REPORT.md`, prove-can-fail
  included), $0.30 for the canary run.
  Ollama is local and costs nothing.

These numbers come from `results/`, which ships with the repo. Your own runs add to it.

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
python3 -m skillevals compare results/A.jsonl results/B.jsonl  # exit 1 on any F1 regression
python3 -m skillevals report                                 # rebuild REPORT.md (trigger runs only)
python3 -m skillevals history                                # rebuild HISTORY.md
bash tests/selftest.sh                                       # offline: unit tests + mutants + lint
```

`prove-can-fail` also works on a set's `draft` (a SKILL.md not installed yet): the draft joins
the catalogue in memory, and its file's sha is checked before and after.

## Outputs

- `results/<date>/<run>.jsonl`: one row per judge call (prompt, expected result, pick, outcome,
  judge, description sha, catalogue sha, cost, latency, error).
- `results/canary/<date>/<run>.jsonl` and `scorecards/<date>/<run>.md`: one canary run (one row
  per agent reply). Canary runs stay out of `HISTORY.md` and `REPORT.md`. The agent's temp
  sandbox path is written as `<sandbox>`, never as the machine's absolute path.
- `scorecards/<date>/<run>/`: one markdown scorecard per skill, plus `_summary.md`.
- `REPORT.md`: the file to read. It shows the latest full baseline per judge, the misroutes,
  the prove-can-fail verdicts and the invalid runs. A run where more than 20% of rows errored
  is not a measurement.
- `HISTORY.md`: every run, one row per skill. A changed `desc sha` means the description changed.

The scorecards, `REPORT.md` and `HISTORY.md` are regenerated from `results/`. Do not edit them by hand.

## Testing the tests

`tests/selftest.sh` runs the unit tests. It then plants 15 known bugs (mutants) in a temp
copy and requires the unit tests to catch each one. Finally it lints the bundled example
skills (`examples/skills`) read-only.
If a mutant survives, the check it targets cannot fail.

## Security and privacy

- No listener, no server. The only outbound traffic is the judge call (`claude -p` to
  Anthropic, or ollama on loopback) and any self-test marked `network` (off by default).
- Nothing from a set's JSON is executed by default. A set's `selftests` are shell commands
  (`bash -c`, your user's rights, cwd `~` unless the set gives one). They run only when you pass
  `--run-selftests` to `lint` or `trigger`; otherwise lint reports them as SKIP. Read a set
  before you opt in.
- No secrets in code or config. The Claude judge runs with the user's existing CLI login.
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

`results/` and `scorecards/` hold the 2026-10-19 runs summarised under Results; your runs add new files beside them.

## License

MIT. See `LICENSE`.
