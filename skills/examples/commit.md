---
name: commit
description: Create a well-formatted git commit from staged changes
when-to-use: When the user wants to commit staged changes, create a commit, or run git commit
argument-hint: "[message hint]"
allowed-tools: run_shell
---

Review the staged changes and create a commit.

Steps:
1. Run `git diff --cached` to see what's staged
2. Run `git status` to see the overall state
3. Write a commit message following this format:
   - First line: imperative mood, under 70 chars (e.g. "Add user auth", "Fix login bug")
   - Blank line
   - Optional body: explain WHY, not WHAT
4. Run `git commit -m "..."` to commit

$ARGUMENTS

Rules:
- Never use --no-verify
- If nothing is staged, tell the user what changes exist and ask what to stage
- Keep the subject line under 70 characters
- Use the present tense, imperative mood ("Add feature" not "Added feature")
