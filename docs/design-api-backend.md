# Design: API backend, three-model comparison, red cases from your own transcripts

Status: design, not built. Decided 2026-10-28 by the repo owner, one decision at a time.
Dates and results from 2026-10-23 refer to the rerun in PR #2 (not yet merged).

API behaviour named here (model ids, prices, no `temperature`, thinking and effort defaults,
`output_config`, refusal `stop_reason`, fallbacks) is taken from Anthropic's API reference as
of October 2026 and is checked against the live docs before each build step.

## Goals

1. An `anthropic:<model>` backend next to `claude -p`, so runs can be billed to an API key
   and `--max-usd` caps real dollars, not a CLI list price.
2. A comparison of Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5 as trigger judge and as canary
   agent.
3. Red test cases mined from your own Claude Code transcripts, so the harness can show that a
   skill needs a fix (red before) and that the fix works (green after), and can rank several
   candidate fixes.

Not goals: changing the existing `claude`, `ollama` or `scripted` backends; sending any
transcript text to a model before it is scrubbed (section 4).

## 1. The `anthropic:` backend

Decision: use the official `anthropic` Python SDK, imported only when an `anthropic:` spec is
used. The core package, the unit tests and the other backends stay standard library only, so
the Python 3.9 CI row is unchanged. The README gains one line: `pip install anthropic` for this
backend.

- Specs: `--judge anthropic:claude-haiku-5-5`, `--agent anthropic:claude-opus-5-5`,
  `--grader anthropic:claude-fable-5-1`. Exact model ids only, no aliases, so every row names
  the model that answered. Row label: `anthropic:<model-id>`.
- Credentials: whatever the SDK resolves (`ANTHROPIC_API_KEY` or a stored profile). The
  harness never reads, logs or stores a key.
- Request shape: no `temperature` (current models reject it). Thinking cannot be turned off
  on Opus 5.5. Effort is a suffix on the spec (`anthropic:claude-opus-5-5@low`), so judge,
  agent and grader can each have their own; it sets `output_config.effort`, and the row label
  carries it (`@default` when unset), so runs at different efforts are never compared
  silently. Default effort differs by model, so a comparison always gives every model the same
  explicit suffix. (Built this way instead of one `--effort` flag, 2026-10-28.)
- The agent gets the same frame as `ClaudeAgent` today: the skill body as system prompt, no
  tools, the same canary prompt. Unlike the CLI agent, it has no sandbox working directory (an
  API call has none).
- Model check: the response's `model` field must equal the requested id; a mismatch is a row
  error, never a verdict.
- Structured output: the judge asks for `{"skill": <one of the catalogue names or ids, or
  "none">}` and the grader for its `grades` array, both through `output_config.format` with a
  JSON schema. This removes the unparseable-output failures (`unparseable reply` from a judge,
  `unparseable grade` from a grader; the haiku grader produced the second in the PR #2 rerun). The CLI backend keeps free-text parsing, so the two are compared knowing this.
- Refusals: a reply with `stop_reason: "refusal"` becomes a row error
  `refusal:<category>`, counted as an error (the run is INCONCLUSIVE), never as a verdict.
  Server-side model fallbacks stay OFF on purpose: a fallback would let a different model
  answer under the requested model's label and spoil the comparison.
- Caching: the router prompt (instructions + catalogue) is identical across a trigger run, so
  it carries `cache_control`. Whether it caches depends on the model's minimum prefix; the
  row records `cache_read_input_tokens`, so the effect is measured, not assumed.

### Real-dollar cap

- `skillevals/prices.py`: a dated table of per-million-token prices (input, output, cache
  write, cache read) for each supported model id. A model missing from the table refuses to
  run under a cap.
- Per call: cost = usage tokens x table price, including thinking tokens (billed as output)
  and cache reads. Stored per row as `cost_usd` with `cost_basis: "api-usage"`. CLI rows keep
  `cost_basis: "cli-list"`, so reports never add the two.
- Before the run: worst case = every planned call (agent calls and every grading's calls) x
  (input tokens x the cache-write price, the highest input rate + `max_tokens` x output
  price). Input tokens come from the API's token-counting endpoint; if that is unavailable,
  the fallback estimate is characters / 2, a deliberately loose bound for code and
  non-English text. If the worst case exceeds
  `--max-usd`, the run refuses to start and prints both numbers.
- During the run: no new call starts once actual spend reaches `--max-usd`, as today. Calls
  already in flight finish, so the overshoot is at most `workers` calls; the preflight check
  bounds that too.

## 2. Three-model comparison

Decision: vary the trigger judge and the canary agent; hold the grader fixed.

- Trigger: each of Haiku 5.5, Sonnet 5.5 and Opus 5.5 judges the bundled sets at one fixed effort suffix (the same for all three) with real names
  and with blind names, two runs each (one run of a model eval is a sample, not a score).
- Canaries: each of the three models is the agent for one run of 14 cases x 3 reps.
- Grader: two gradings of every canary run. Grading 1 is Claude Fable 5.1, which is not one of
  the models under test. Grading 2 is Opus 5.5. A run is certified only when both gradings
  agree (the two-gradings rule from the PR #2 rerun, written into the README there). If Opus favours replies from the Opus agent, it shows
  as Opus disagreeing with Fable on those rows, which the report counts per agent.
- Baseline: only runs graded by the current grader count. Earlier rows graded by the old
  grader (the 41/42 canary result of 2026-10-19) are history, not a comparison row.
- Report: one table per role, model by model, with every run listed (no best-of), the cost
  per run in real dollars, and the grader agreement rate per agent.
- Prove-can-fail runs once per judge model, so each model's scores come with evidence that it
  can see a broken description.

The first paid run measures cost per call per model; estimates in this document are not
measurements.

## 3. Workspace

Decision: a `--workspace DIR` flag. Results, scorecards, `HISTORY.md` and `REPORT.md` are
written under `DIR` instead of the repo. The default stays the repo, so public behaviour does
not change. A unit test runs a scripted trigger and canary run with a temp workspace and
asserts that the repo tree is byte-for-byte unchanged.

Private runs (your own skills, your own cases) always use a workspace outside the repo.

## 4. Red cases from your own transcripts

Decision: scrub, then any model. Raw transcript text never reaches a remote model, through the
API or `claude -p`, until it has been scrubbed and a person has reviewed the scrubbed case. The
only model that sees raw text is the local scrub model in step 2.

Pipeline, all local until step 4:

1. Mine. A local scanner reads Claude Code transcripts (`~/.claude/projects/**/*.jsonl`) and
   flags three signals:
   - a skill fired and the next user turn corrects it (wrong skill, "no", "not that");
   - a skill did not fire and the user then invoked it by hand (`/name`);
   - a step of a skill run that the user interrupted or reversed.
   Each hit keeps the user prompt, the skill involved and the signal. Tool output in a
   transcript counts as raw text too. Hits stay in the workspace, never in the repo.
2. Scrub. Each hit is rewritten into a case that keeps the routing problem and drops
   everything personal: names, people, places, amounts, paths, message text. The rewrite is
   drafted by a local model (Ollama bound to 127.0.0.1: check that `OLLAMA_HOST` is not a
   remote or 0.0.0.0 address; if the harness started the server it stops it after, and it
   never stops one that was already running). Then two deterministic checks reject any case
   that still matches: a secret scanner (API keys, tokens, private keys) and a deny list
   (emails, URLs, hostnames, phone numbers, paths, money, a private names list kept in the
   workspace).
3. Review. A person reads each scrubbed case next to its raw hit and accepts, edits or drops it.
   Only accepted cases move on.
4. Run. Accepted cases join a copy of the skill's set inside the workspace as
   `"source": "mined"` cases and can run on any backend. The public `sets/` files never
   receive a mined case.

### Red before, green after

For one skill and its accepted mined cases:

- Red: the current description fails the case in at least 2 of 3 samples. One sample is not
  enough: across the full haiku runs in PR #2, three bundled prompts flipped between runs.
- Fix: a candidate description passed with `--override skill=FILE` (in memory; the skill folder
  is never written).
- Green: the candidate passes the case in 3 of 3 samples, and the skill's existing set (the guard set) loses
  no case that the baseline passed in 3 of 3 samples. The prove-can-fail tolerance (0.15, a
  noise margin for restored-vs-baseline) is not used here: as a regression bar it would let a
  real 0.15 F1 drop pass.
- Proof the check can fail: the same run with the description broken (`--break-skill`) must
  turn the mined cases red. Without that, a green result proves nothing.
- Benchmarking fixes: several candidates run against the same red cases and guard set; the
  report ranks them by mined-case pass rate, then guard F1, then description length.

Whether any scrubbed red-to-green example goes into this README as a showcase is a separate
decision, taken after real ones exist.

## Build order

`--override` and `--break-skill` exist for trigger runs only today; the red/green command
uses those. Each step is its own commit with tests first; the unit tests stay offline (scripted backends
and a fake SDK client).

1. `--workspace` and its repo-unchanged test.
2. `prices.py` and the real-dollar preflight and mid-run cap, tested with a fake client.
3. The `anthropic:` judge, agent and grader, with structured output and refusal handling.
4. The comparison plan command and its report table.
5. The transcript miner and scrub check (local, no model calls), then the red/green command.

No paid run happens without the owner's go, with the preflight's worst-case number shown first.
