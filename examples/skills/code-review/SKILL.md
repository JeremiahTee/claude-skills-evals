---
name: code-review
description: "Use when the user asks to review a diff, a branch or a pull request for bugs, risky changes or missing tests. Do NOT use for explaining code simply (use eli5) or for planning commits (use gen-commits)."
---

# code-review (example fixture)

An invented example skill, shipped so the trigger evals have something to route to.

1. Read the full diff, not just the summary.
2. Report correctness bugs first, each with file, line and a failing input.
3. Then risky changes and missing tests. Skip style nits unless asked.
