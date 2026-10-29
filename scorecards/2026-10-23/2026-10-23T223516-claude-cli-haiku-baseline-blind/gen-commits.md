# Scorecard: `gen-commits`

- Run: `2026-10-23T223516-claude-cli-haiku-baseline-blind` · variant **baseline-blind**
- Judge: **claude-cli:haiku**
- Description sha: `c51c50f54a61` · catalogue sha: `a5b2a1a9f808`
- Accepted as a hit: gen-commits

## Trigger eval

| precision | recall | F1 | accuracy | TP | FP | TN | FN | errors | cost USD |
|---|---|---|---|---|---|---|---|---|---|
| 1.00 | 1.00 | 1.00 | 1.00 | 6 | 0 | 4 | 0 | 0 | 0.0017 |

| case | kind | expect | picked | result | prompt |
|---|---|---|---|---|---|
| gen-commits-01 | positive | trigger | gen-commits | ok | /gen-commits |
| gen-commits-02 | positive | trigger | gen-commits | ok | I've got a bunch of unrelated changes in the working tree, group them into commits |
| gen-commits-03 | positive | trigger | gen-commits | ok | give me a commit plan for this repo |
| gen-commits-04 | positive | trigger | gen-commits | ok | stage plan please: tools, tests and docs all changed |
| gen-commits-05 | positive | trigger | gen-commits | ok | organize my uncommitted changes into logical commits for main |
| gen-commits-06 | positive | trigger | gen-commits | ok | commit list? |
| gen-commits-07 | near-miss | no | none | ok | just commit the README typo fix |
| gen-commits-08 | near-miss | no | none | ok | fix the message on my last commit, it has a typo |
| gen-commits-09 | near-miss | no | none | ok | I have merge conflicts in package-lock.json after the rebase, help me resolve them |
| gen-commits-10 | unrelated | no | none | ok | what's the weather tomorrow? |

## Deterministic checks

| check | level | detail |
|---|---|---|
| frontmatter-parses | PASS |  |
| name-matches-dir | PASS |  |
| description-length | PASS | 282 chars (limit 1024) |
| description-says-when | PASS |  |
| description-has-negatives | PASS |  |
| cited-paths-exist | PASS | 0/0 exist |
