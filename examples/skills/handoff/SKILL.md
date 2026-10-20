---
name: handoff
description: "Use when the user is leaving a session and another session must pick the work up cold: context is nearly full, \"/handoff\", \"save and continue\", switching machines, or a prompt to continue in a fresh session. Do NOT use for an end-of-session status check (use wrap), for resuming earlier work (use resume), for the daily log (use journal), or for lessons that outlive the task (use extract-learnings)."
---

# handoff (example fixture)

An invented example skill, shipped so the trigger evals have something to route to.

Write a continuation prompt another session can run cold:

1. Goal, in one line.
2. State: what is done, with file paths and commit hashes.
3. What is not done, in priority order.
4. The exact next command to run.
5. Traps already found, so the next session does not repeat them.

Print it in chat inside one fenced block, ready to paste after a clear.
