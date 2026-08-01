---
description: Take the current change through verify, review, commit, and release-note prep
argument-hint: "[optional: short description of the change]"
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Task
---

Take the current working tree through the ship sequence from the `build-workflow`
skill. Change description (if given): `$1`

Do not skip steps. Stop and report at the first hard failure rather than
continuing past a red gate.

## 1. Verify

Run the full local gate:

```bash
uv run ruff format --check . && \
uv run ruff check . && \
uv run mypy src && \
uv run python scripts/check_layers.py && \
uv run pytest -q --cov=src --cov-branch --cov-report=term-missing --cov-fail-under=85 && \
uv run bandit -q -ll -r src && \
uv run pip-audit
```

If anything fails: report it and stop. Do not lower a threshold to proceed.

## 2. Review

Invoke the **code-reviewer** agent on the diff. Invoke **architecture-reviewer**
too if the change adds a module, adds a dependency, crosses a layer boundary, or
changes the data model or API contract.

Address blocking findings before continuing. If a finding is being dismissed,
state the reason explicitly.

## 3. Definition of Done

Walk the DoD checklist from the `build-workflow` skill and mark each item. Pay
particular attention to: *did a test exist that failed before this change?* If
this is a bug fix without a regression test, stop.

## 4. Commit

Group the changes into logical commits — one concern each. For each, write a
**Conventional Commit** message per the `release-notes` skill:

- Correct type (`feat`/`fix`/`perf`/`refactor`/`docs`/`test`/`chore`/`build`/`ci`)
- Scope where meaningful
- Imperative, lower case, no trailing period
- `!` plus a `BREAKING CHANGE:` footer with a migration path, if applicable

Show the proposed messages to the user before committing. The pre-commit guard
hook will run — if it blocks, fix the finding rather than bypassing it.

## 5. Release impact

Determine the SemVer impact of what is being shipped (MAJOR/MINOR/PATCH/none)
and draft the `[Unreleased]` CHANGELOG.md entries, written for a user audience
rather than copied from commit subjects.

## 6. PR body

Produce a PR description:

```markdown
## What
## Why
## How it was verified
## Rollback plan
## Breaking changes
(or "None")
```

Report what was committed, the SemVer impact, and anything left outstanding.
