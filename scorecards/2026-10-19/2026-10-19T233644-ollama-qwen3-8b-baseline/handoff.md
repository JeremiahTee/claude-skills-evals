# Scorecard: `handoff`

- Run: `2026-10-19T233644-ollama-qwen3-8b-baseline` · variant **baseline**
- Judge: **ollama:qwen3:8b (PROXY)**
- Description sha: `4fabea77c3a4` · catalogue sha: `a5b2a1a9f808`
- Accepted as a hit: handoff

## Trigger eval

| precision | recall | F1 | accuracy | TP | FP | TN | FN | errors | cost USD |
|---|---|---|---|---|---|---|---|---|---|
| 1.00 | 1.00 | 1.00 | 1.00 | 7 | 0 | 5 | 0 | 0 | 0.0000 |

| case | kind | expect | picked | result | prompt |
|---|---|---|---|---|---|
| handoff-01 | positive | trigger | handoff | ok | Context is almost full. Write me a handoff so I can /clear and keep going on the parser refactor. |
| handoff-02 | positive | trigger | handoff | ok | the context window is nearly full, write the continuation note before I clear |
| handoff-03 | positive | trigger | handoff | ok | save and continue, I have to jump to the work laptop in the middle of this migration |
| handoff-04 | positive | trigger | handoff | ok | give me a prompt to continue this in a fresh session |
| handoff-05 | positive | trigger | handoff | ok | we're at 90% context, clear context and carry on after |
| handoff-06 | positive | trigger | handoff | ok | /handoff |
| handoff-07 | positive | trigger | handoff | ok | before I switch machines, write the continuation prompt for this task with the exact next commands |
| handoff-08 | near-miss | no | wrap | ok | wrap up before clear: finish pending tasks and save my notes and memory |
| handoff-09 | near-miss | no | journal | ok | draft today's journal from my sessions |
| handoff-10 | near-miss | no | resume | ok | where was I? pick up yesterday's work |
| handoff-11 | near-miss | no | extract-learnings | ok | extract learnings from this bug fix before we move on |
| handoff-12 | unrelated | no | none | ok | what's the weather tomorrow? |

## Deterministic checks

| check | level | detail |
|---|---|---|
| frontmatter-parses | PASS |  |
| name-matches-dir | PASS |  |
| description-length | PASS | 400 chars (limit 1024) |
| description-says-when | PASS |  |
| description-has-negatives | PASS |  |
| cited-paths-exist | PASS | 0/0 exist |
