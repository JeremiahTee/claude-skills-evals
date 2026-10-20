---
name: gen-commits
description: "Use when the working tree holds several unrelated changes that should be grouped into logical commits, or the user asks for a commit plan, a stage plan or a commit list. Do NOT use for a single obvious one-file commit, for amend-only message fixes, or for merge-conflict resolution."
---

# gen-commits (example fixture)

An invented example skill, shipped so the trigger evals have something to route to.

1. Run `git status --short` and `git diff --stat` to see every changed file.
2. Group the files by intent (feature, fix, tests, docs, chore). One intent per commit.
3. Print the plan as a numbered list: commit message, then the files it stages.
4. Wait for the user's go before staging or committing anything.

Commit messages use a type prefix (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) and a short
imperative subject.
