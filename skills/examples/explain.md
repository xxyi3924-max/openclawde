---
name: explain
description: Explain how a piece of code or system works
when-to-use: When the user asks "how does X work", "explain this", or wants to understand code
argument-hint: "[file, function, or concept to explain]"
allowed-tools: read_file, grep_files, list_files
---

Explain the following clearly and concisely:

$ARGUMENTS

How to explain:
1. Start with a one-sentence summary of what it does
2. Describe the key components or flow
3. Point out any non-obvious or interesting design decisions
4. If there's a simpler mental model, give it

Tailor the explanation to the level of detail the user seems to want.
Don't over-explain obvious things; focus on what's actually complex or surprising.
