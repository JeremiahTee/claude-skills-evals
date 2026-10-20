---
name: notes-search
description: "Use when the user asks what they already know about a topic from their own notes: \"what do I know about X\", \"search my notes for Y\", \"did I write anything about Z\". Do NOT use for adding new notes or for explaining a topic from scratch (use eli5)."
---

# notes-search (example fixture)

An invented example skill, shipped so the trigger evals have something to route to.

1. Search the user's notes folder for the topic and close synonyms.
2. Return the matching notes, newest first, each with its path and a one-line quote.
3. Say plainly when nothing matches. Never fill the gap from general knowledge.
