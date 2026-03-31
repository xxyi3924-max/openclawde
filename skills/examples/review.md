---
name: review
description: Review code changes for bugs, style, and improvements
when-to-use: When the user wants a code review, wants to review a diff, or asks "review this"
argument-hint: "[file or diff to review]"
allowed-tools: run_shell, read_file, grep_files
context: fork
---

Perform a thorough code review of the specified changes.

$ARGUMENTS

Review for:
1. **Bugs** — logic errors, off-by-one, null/none handling, race conditions
2. **Security** — injection, hardcoded secrets, insecure defaults, input validation
3. **Quality** — readability, naming, duplication, unnecessary complexity
4. **Performance** — obvious inefficiencies, missing indexes, N+1 queries

Format your response as:
- Summary: one-sentence overall assessment
- Issues: bulleted list of specific problems (HIGH/MEDIUM/LOW severity)
- Suggestions: optional improvements (not blockers)
- Verdict: APPROVE / REQUEST CHANGES / NEEDS DISCUSSION
