# Scorecard: `gen-commits`

- Run: `2026-10-19T233902-claude-cli-haiku-broken-wrong-blind` · variant **broken-wrong-blind**
- Judge: **claude-cli:haiku**
- Description sha: `224d2dca2c82` · catalogue sha: `4c5f3dbe37eb`
- Accepted as a hit: gen-commits

## Trigger eval

| precision | recall | F1 | accuracy | TP | FP | TN | FN | errors | cost USD |
|---|---|---|---|---|---|---|---|---|---|
| n/a | 0.00 | 0.00 | 0.40 | 0 | 0 | 4 | 6 | 0 | 0.0023 |

| case | kind | expect | picked | result | prompt |
|---|---|---|---|---|---|
| gen-commits-01 | positive | trigger | none | WRONG (missed) | /gen-commits |
| gen-commits-02 | positive | trigger | none | WRONG (missed) | I've got a bunch of unrelated changes in the working tree, group them into commits |
| gen-commits-03 | positive | trigger | code-review | WRONG (missed) | give me a commit plan for this repo |
| gen-commits-04 | positive | trigger | none | WRONG (missed) | stage plan please: tools, tests and docs all changed |
| gen-commits-05 | positive | trigger | none | WRONG (missed) | organize my uncommitted changes into logical commits for main |
| gen-commits-06 | positive | trigger | none | WRONG (missed) | commit list? |
| gen-commits-07 | near-miss | no | none | ok | just commit the README typo fix |
| gen-commits-08 | near-miss | no | none | ok | fix the message on my last commit, it has a typo |
| gen-commits-09 | near-miss | no | none | ok | I have merge conflicts in package-lock.json after the rebase, help me resolve them |
| gen-commits-10 | unrelated | no | none | ok | what's the weather tomorrow? |

## Misroutes to read

- `gen-commits-01` FN: went to `none`: "/gen-commits"
- `gen-commits-02` FN: went to `none`: "I've got a bunch of unrelated changes in the working tree, group them into commits"
- `gen-commits-03` FN: went to `code-review`: "give me a commit plan for this repo"
- `gen-commits-04` FN: went to `none`: "stage plan please: tools, tests and docs all changed"
- `gen-commits-05` FN: went to `none`: "organize my uncommitted changes into logical commits for main"
- `gen-commits-06` FN: went to `none`: "commit list?"
