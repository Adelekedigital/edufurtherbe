---
description: Root-cause a failure using the hypothesis-driven debugging method
argument-hint: "<failing test, error message, or description of the wrong behaviour>"
allowed-tools: Bash, Read, Grep, Glob, Edit, Task
---

Diagnose and fix: `$1`

Delegate to the **debugger** agent, which owns the method. Give it everything it
needs to reproduce:

- The exact symptom as reported above
- The failing command or endpoint, if known
- Recent changes: `git log --oneline -20` and `git diff HEAD~1 --stat`
- Relevant log output or traceback

Require its full report format back: symptom, root cause with `file:line`,
evidence, minimal fix, regression test, and related risk.

Before accepting the diagnosis, confirm two things yourself:

1. The regression test was **observed failing** against the unfixed code, with
   the same symptom described above. Not "would fail" — actually run and seen.
2. The full local gate is green after the fix.

If the agent could not reproduce the issue, do not let it propose a speculative
fix. Report what was ruled out and what information is needed.
