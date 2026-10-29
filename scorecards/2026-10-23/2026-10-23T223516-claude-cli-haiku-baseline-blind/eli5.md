# Scorecard: `eli5`

- Run: `2026-10-23T223516-claude-cli-haiku-baseline-blind` · variant **baseline-blind**
- Judge: **claude-cli:haiku**
- Description sha: `a163880351d0` · catalogue sha: `a5b2a1a9f808`
- Accepted as a hit: eli5

## Trigger eval

| precision | recall | F1 | accuracy | TP | FP | TN | FN | errors | cost USD |
|---|---|---|---|---|---|---|---|---|---|
| 1.00 | 1.00 | 1.00 | 1.00 | 6 | 0 | 6 | 0 | 0 | 0.0025 |

| case | kind | expect | picked | result | prompt |
|---|---|---|---|---|---|
| eli5-01 | positive | trigger | eli5 | ok | /eli5 3 |
| eli5-02 | positive | trigger | eli5 | ok | eli5 this, i dont get the half-open part of the circuit breaker |
| eli5-03 | positive | trigger | eli5 | ok | explain like I'm 5 how the sqlite log renders the markdown file |
| eli5-04 | positive | trigger | eli5 | ok | i dont understand finding 2, what does 'lease' mean there? |
| eli5-05 | positive | trigger | eli5 | ok | what does WAL mode actually mean in that db connect function? dumb it down |
| eli5-06 | positive | trigger | eli5 | ok | that explanation went over my head, can you break the retry backoff down simply |
| eli5-n1 | near-miss | no | extract-learnings | ok | what did we learn today? capture the insights from this session |
| eli5-n2 | near-miss | no | extract-learnings | ok | extract learnings from the bug fix we just finished |
| eli5-n3 | near-miss | no | notes-search | ok | what do I know about sqlite from my notes? |
| eli5-n4 | near-miss | no | code-review | ok | review the diff on this branch for bugs |
| eli5-n5 | near-miss | no | journal | ok | journal today, log my day |
| eli5-n8 | near-miss | no | handoff | ok | context is full, write a handoff so I can clear and continue |

## Deterministic checks

| check | level | detail |
|---|---|---|
| frontmatter-parses | PASS |  |
| name-matches-dir | PASS |  |
| description-length | PASS | 392 chars (limit 1024) |
| description-says-when | PASS |  |
| description-has-negatives | PASS |  |
| cited-paths-exist | PASS | 0/0 exist |
