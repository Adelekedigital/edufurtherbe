---
description: Run the full pre-merge review gate on the current diff
argument-hint: "[optional: base branch, defaults to main]"
allowed-tools: Bash, Read, Grep, Glob, Task
---

Run the complete pre-merge review gate against the current change set.

Base for comparison: `$1` (default to `main` if empty).

## 1. Establish the diff

```bash
git status --short
git diff --stat $(git merge-base HEAD ${1:-main})...HEAD
```

If there is no diff, say so and stop.

## 2. Run the automated gate

Run each, and report the results as a table. Do not stop at the first failure —
the user wants the full picture in one pass.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run python scripts/check_layers.py
uv run pytest -q --cov=src --cov-branch --cov-fail-under=85
uv run bandit -q -ll -r src
uv run pip-audit
```

## 3. Run the review agents in parallel

Launch these concurrently in a single message:

- **code-reviewer** on the diff — correctness, security, tests, error handling
- **architecture-reviewer** — but only if the diff adds a module, adds a
  dependency, crosses a layer boundary, or changes the data model or public API.
  Skip it for a localized fix and say why you skipped it.

## 4. Apply the skill checklists the diff triggers

Each skill owns its own criteria — apply the checklist there, do not restate it
here.

| If the diff touches | Apply |
|---|---|
| Auth, user input, SQL, uploads, outbound requests, PII | `security-checker` |
| Data one customer must not see from another | `tenant-isolation` *(if installed)* |
| An endpoint or response shape | `api-designer` — and state explicitly whether the change is **breaking** |
| A migration | `db-migration` — the question that decides it: is this safe with old **and** new code running at once? |
| Models, sessions, queries, identifiers | `persistence-patterns` |
| Branching, merging, promotion | `deploy-workflow` |
| Anything the project decides for itself | `project-conventions` + its failure-modes log |

## 4b. The PR Review Procedure

Run it from `build-workflow`, against the **actual diff** — not the PR
description, not from memory. In particular:

- **Hunt the partner-fix.** A condition widened in one query and not its sibling
  is the classic half-fix.
- **Confirm the tests fail without the fix.** A test that passes either way locks
  nothing in.
- **New pattern, new rule.** If this introduces a pattern the codebase has not
  used, `project-conventions` needs a row, or the next person re-decides it
  differently.

## 5. Consolidated report

Produce one report:

```
## Gate
| Check | Result |
|---|---|
| ruff format | ✅ / ❌ |
| ruff check | ... |
| mypy | ... |
| layer boundaries | ... |
| pytest + coverage | ... |
| bandit | ... |
| pip-audit | ... |

## Blocking findings
Merged and deduplicated across all sources, each with file:line and a fix.

## Non-blocking findings

## Definition of Done
Check the build-workflow skill's DoD list and mark each item.

## Verdict
READY TO MERGE | CHANGES REQUIRED
```

Be direct about the verdict. If the gate is red, it is CHANGES REQUIRED
regardless of how minor the failure looks.
