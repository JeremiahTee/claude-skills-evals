---
name: eli5
description: "Use when the user asks for something to be explained simply (\"eli5\", \"explain like I'm 5\", \"dumb it down\", \"/eli5 N\" for a numbered finding), or says an explanation, a finding or a piece of code went over their head. Do NOT use for capturing what a session taught (use extract-learnings), for looking up what the user already noted (use notes-search), or for reviewing code (use code-review)."
---

# eli5 (example fixture)

An invented example skill, shipped so the trigger evals have something to route to.

Explain one thing in plain words, in this order:

1. The one-sentence answer.
2. An everyday analogy.
3. What it looks like in the user's own code or text.
4. The one detail people usually get wrong.
5. A two-line check the user can run to see it for themselves.

Keep it under 250 words. Ask which part is still unclear rather than adding more.
