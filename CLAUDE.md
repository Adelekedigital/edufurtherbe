# CLAUDE.md

Router for agents working in this repository.

> **Keep this file short.** It loads on every turn, so anything added here is a
> permanent context cost paid by every task, relevant or not. Depth belongs in a
> skill, which loads only when it applies. If a section here grows past a few
> lines, that is the signal to move it into `.claude/skills/`.

## Two tiers

| Tier | Where | Who owns it |
|---|---|---|
| **Generic standards** | `.claude/skills/` (except `project-conventions`) | The shared package. Overwritten on update — do not edit. |
| **This project** | `project-conventions` skill + this file | You. Never overwritten. |

Anything true only here goes in `project-conventions`, never in a generic skill.

## Start here

| Situation | Use |
|---|---|
| Any non-trivial change | `build-workflow` — six steps, no skipping |
| How *this* project does it | `project-conventions` |
| Deciding where a file goes | `project-structure` |
| Models, queries, sessions, ids | `persistence-patterns` |
| Writing tests | `test-writer` |
| Adding or changing an endpoint | `api-designer` |
| Any schema change | `db-migration` — expand/contract, never one release |
| What the new schema should be | `docs/edufurther-migration/` — canonical, never edited here (ADR 0007) |
| Auth / input / SQL / PII touched | `security-checker` |
| Data one customer must never see from another | `tenant-isolation` *(if installed)* |
| Adding logs or metrics | `observability` |
| Branching, merging, promoting | `deploy-workflow` |
| Cutting a release | `release-notes` |
| A consequential choice | `adr`, then `/adr-new` |
| Something is broken | `/debug <symptom>` |
| Before opening a PR | `/review` |

## Commands

```bash
uv sync                        # install (frozen in CI)
docker compose up -d           # local Postgres on 55432; tests marked `db` skip without it
uv run alembic upgrade head    # apply migrations — never on app startup
uv run uvicorn app.main:app --reload --app-dir src
uv run pytest -q               # full suite
make check                     # the full local gate — before every commit
```

## Non-negotiables

1. **`domain/` imports no framework and no other layer except `core/`.** Enforced
   by `scripts/check_layers.py`. Do not weaken the config to pass.
2. **The checklist and Definition of Done are approved before implementation** —
   as text, with no tool calls in the same response.
3. **Watch every test fail before making it pass.** And assert the positive case,
   not only the negative.
4. **Never lower a coverage, lint, or severity threshold to go green.** If a
   threshold is wrong, say so and ask.
5. **Object-level authorization is scoped in the query**, on every read *and*
   write path — never checked after fetching.
6. **All configuration flows through `core/config.py`.** No inline `os.environ`.
   Secrets are `SecretStr` and never logged.
7. **Deep checks report findings only.** Never edit during a review, a debug, or
   an audit until the fix and its impact are approved.
8. **Conventional Commits.** The release tooling parses them.
9. **Every table has `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`** (ADR
   0014) — no natural keys, no composite keys, no caller-supplied ids. An
   invariant a natural or composite key would have carried is re-declared as
   `UNIQUE`. An exception is an ADR superseding 0015, never a local judgement:
   this rule already lived in `persistence-patterns` and was overridden twice
   with every gate green.

## Layout

```
src/app/api/       transport only — no business logic
src/app/domain/    the product — pure Python, no I/O, no framework
src/app/infra/     adapters — DB, cache, outbound clients
src/app/core/      config and base errors
tests/{unit,integration,e2e}/
migrations/        Alembic chain — outside src/, so unpackaged and unscanned
docs/adr/          decision records — read before proposing a rewrite
```

`api/deps.py` and `main.py` are the only sanctioned wiring points.

## Before saying it's done

Run the Build Verification Gate in `build-workflow`. All of it, not most of it.
End with the explicit verdict — or say what you found.
