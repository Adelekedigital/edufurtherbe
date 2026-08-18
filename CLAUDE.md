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
2. **The checklist and Definition of Done are written before implementation.**
   ~~And approved, as text, with no tool calls in the same response.~~ —
   **amended by rule 7 below:** they are still written first, at every tier, and
   none of them waits for approval. `build-workflow` is tier 1 of the standards
   package and still says wait; this file overrides it, which is what tier 2 is
   for.
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
8. **One rule, one representation.** A predicate, mapping, constant or test
   double that exists in a second place is a **defect**, not a style question —
   extract it, or pin the copies with a test that fails when they diverge. Every
   duplication this project has shipped was found by a human after a copy was
   missed; no gate sees them.
9. **Conventional Commits.** The release tooling parses them.
10. **Every table has `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`** (ADR
    0015) — no natural keys, no composite keys, no caller-supplied ids. An
    invariant a natural or composite key would have carried is re-declared as
    `UNIQUE`. An exception is an ADR superseding 0015, never a local judgement:
    this rule already lived in `persistence-patterns` and was overridden twice
    with every gate green.

## How we work

Nine rules. The first six came out of a retrospective on M1 — each is there
because it cost real time, and that count is in `failure-modes.md`. Rules 7–9 were
added 2026-08-17, when the approval gate itself turned out to be one of the costs.

1. ~~**Never stack pull requests.**~~ **Reversed 2026-08-16, deliberately.**
   Stacking is allowed: branch from the PR you build on, merge in order. The
   original rule cost more than the stacks did — the enum-to-text conversion is
   eight sequential migrations, and one-at-a-time made it eight round trips.
   What stands from the old rule is *why* it existed: every stack so far needed
   rebase surgery. So the price is named rather than wished away — **Alembic's
   chain is linear**, so two open PRs each adding a migration off the same head
   produce two heads and `alembic upgrade head` fails outright. A stacked
   migration PR fixes its `down_revision` at merge time, in merge order. Set the
   PR base to the branch below it so the diff stays readable; GitHub retargets to
   `main` when the parent merges.
2. **An ADR lands `Accepted` in the PR that implements it.** `Proposed` plus a
   second acceptance PR is for a decision that genuinely blocks someone else —
   not the default. Two PRs per decision was pure overhead.
3. **Record a decision only if reversing it later is expensive *and* a competent
   engineer would be surprised.** Otherwise a settled-decision row is enough.
   Eighteen ADRs before the second endpoint is a signal, not an achievement.
4. **Verify before asserting or acting.** One command to check the branch, the
   file, or the platform's documentation — before claiming state, and before
   writing code against a tool's behaviour. This rule alone would have prevented
   an entire merged-then-deleted PR and two false "main is broken" reports.
5. **Decide only where the answer is already settled** — the repo says it, or the
   same pattern was handled before. ~~Anything that is genuinely a choice comes
   back for approval.~~ — **narrowed by rule 8 below:** what comes back is what
   *blocks*. Two defensible options is not that; take the one the existing pattern
   implies and state the assumption. Speed is still not a reason to decide on
   someone's behalf — rule 8 changes which questions are worth a round trip, not
   whose call it is.
6. **Report in plain words, short.** Verdict first, evidence in a table, prose
   only for the one thing that matters. If the reader cannot act on it in under a
   minute, it is too long — and a decision nobody can follow is a decision nobody
   can make.

7. **Standing approval at every tier.** Write the checklist and Definition of
   Done, post them, and **continue in the same response** — they are the build's
   specification and its verification contract, not a request. Amends
   non-negotiable #2, which stopped at every tier and cost a round trip on
   renames and docs.

   ~~Tier 1 still stops for approval before implementation.~~ **Widened
   2026-08-18, on evidence rather than preference.** Four consecutive Tier 1
   pauses changed the outcome **once**: a migration that could not carry a value
   faithfully and needed a human to choose between quarantining it, relaxing the
   constraint it violated, and silently rewriting the data. That question
   qualified under rule 8 on its own terms, so it would have been asked with no
   blanket pause in place; the other three cost a round trip and came back
   "approved as written". **What protects a migration is rule 8's escalation bar
   and rule 9's guarantees, not the tier.** The tier still decides *verification
   depth*, which is what it was for.

   So a Tier 1 build posts its checklist and proceeds — and stops the moment it
   finds something rule 8 qualifies, which for a migration routinely includes
   data that cannot be carried faithfully, a backfill whose source is ambiguous,
   and anything irreversible once applied to real rows.
8. **Escalate only what blocks.** A question qualifies if proceeding either way
   would be unsafe, would need rework to undo, or would commit a public contract
   to a shape that is breaking to change. It does **not** qualify because two
   options both look reasonable, because a settled decision does not name this
   exact case, or because the codebase has no precedent — take the option the
   existing pattern implies, state the assumption in the PR body, and keep
   building. When something genuinely does qualify, **batch it**: hold it until
   the tier's natural pause and ask everything at once, in the #50 shape.
9. **Unchanged by 7 and 8**, because these are what make the speed safe: watch
   every test fail before it passes; no threshold lowered to go green;
   object-level authorization scoped in the query on every read and write path;
   `security-checker` run whenever auth, input, SQL or PII is touched; the Build
   Verification Gate in full before any work is called done; and a red gate is
   fixed in the build that found it (#99).

Working alone in the repository is assumed. When another session may be active,
use `git worktree` rather than switching the shared checkout.

### How much verification

Depth scales with what a mistake costs. **Every tier still gets a checklist and
a Definition of Done before implementation** — the tier decides verification
depth and nothing else. Since rule 7 was widened, **no tier waits for approval**:
the build posts its checklist and proceeds, and escalates only what rule 8
qualifies.

| Tier | What | Verification |
|---|---|---|
| 1 | Irreversible or touching real data — ETL, migrations, auth, provisioning | Watch-fail, mutation batch, stress against real data, review agents |
| 2 | A normal feature, endpoint, or module; anything production reads | Tests + full gate; mutations on the new guard only |
| 3 | Mechanical — renames, moves, docs | Gate only |

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

`api/deps.py` and `main.py` are the sanctioned wiring points inside `src/`.
`scripts/*.py` is a third composition root — it may construct concrete `infra`
classes, and it may hold **no business rules and no SQL** (see
`project-conventions`, which explains why the gates cannot see it).

## Before saying it's done

Run the Build Verification Gate in `build-workflow`. All of it, not most of it.
End with the explicit verdict — or say what you found.
