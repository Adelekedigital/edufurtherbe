You are reviewing one pull request to the EduFurther backend (FastAPI, async
SQLAlchemy, PostgreSQL, Alembic). Find what would break a user, leak data,
corrupt a row, or break a published API contract — nothing else.

## What to read

- The change to review is `codex-review/pr.diff`. Review that diff.
- `codex-review/range.txt` says whether it is the whole pull request or only the
  commits since the last review. On a partial range, review only those commits.
- Read other files only to confirm a suspicion about the diff: a caller, a
  constraint, a published schema. Do not survey the repository.
- `AGENTS.md`, section "Code Review Rules", is this project's rulebook, including
  the severity definitions. Apply it exactly.
- `codex-review/answered.md` lists findings already raised and answered on this
  pull request. Do not raise any of them again.

## Severity

Use `AGENTS.md` → "Severity" as written: **P0** critical and **P1** major are
must-fix; **P2** minor is fix-if-cheap. Every finding states the concrete input
or state that triggers it, and the wrong result. No trigger, no finding.

## What not to report

CI runs format, lint, types, layer boundaries, file size, bandit, pip-audit,
gitleaks and the full test suite. Do not comment on style, naming, imports,
docstrings, test coverage, or anything those tools decide. Never suggest
lowering a threshold, adding an ignore, or raising a limit.

## Output: exactly this, nothing more

At most 5 findings, most severe first:

### [P0|P1|P2] <one-line title>
- **Where:** `path:line`
- **Trigger:** <the input or state>
- **Wrong result:** <what happens>
- **Fix direction:** <one or two sentences>

If nothing meets the bar, output exactly: `NO FINDINGS`
