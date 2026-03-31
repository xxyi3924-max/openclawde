---
name: debug
description: Systematically debug an error or unexpected behavior
when-to-use: When the user reports a bug, error, or unexpected behavior and wants it fixed
argument-hint: "[error message or description]"
allowed-tools: run_shell, run_python, read_file, grep_files, edit_file
---

Debug and fix the following issue:

$ARGUMENTS

Debugging process:
1. **Reproduce** — run the code to confirm the error
2. **Locate** — grep for relevant code, read the stack trace carefully
3. **Hypothesize** — form a theory about root cause before changing anything
4. **Fix** — make the minimal change that fixes the root cause (not the symptom)
5. **Verify** — run again to confirm the fix works
6. **Report** — explain what was wrong and what you changed

Do not:
- Change unrelated code while fixing a bug
- Add workarounds that mask the real problem
- Assume the fix worked without verifying
