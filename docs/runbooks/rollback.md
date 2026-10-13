# Runbook: rollback

A release is bad. Decide whether it is the **code** or the **schema**, because
they roll back differently and the code is almost always the answer.

## Decide first

| Symptom | Do |
|---|---|
| New code misbehaves, migration (if any) is fine | Roll back the app (below). Leave the schema |
| A migration failed part-way or timed out on a lock | Re-dispatch Migrate (below) |
| A migration applied and is itself wrong | Roll forward with a fixing migration. `alembic downgrade` is the last resort |
| Data is wrong or lost | Stop writes, then [`backup-restore.md`](backup-restore.md) |

## Roll back the app

1. In the Railway console, redeploy the **previous successful deployment** of
   the production service. **Owner step**: nothing in the repo drives Railway
   (no `railway.json`), and this runbook does not use the Railway CLI.
2. This is safe **only if the previous code runs on the current schema**. The
   project's rule is that it must: expand/contract, each phase a separate
   release, "You must be able to roll back to the previous release at every
   point" (`.claude/skills/db-migration/SKILL.md`, Expand / Contract).
3. Know the one direction that bites. "Expand-only means the *old* code
   tolerates the new schema; it does not mean new code tolerates an old one"
   (`project-conventions`, migration ordering). Rolling the **app** back over an
   expand migration is safe. Rolling the **schema** back under new code is not.
4. If the release you are leaving was a **contract** step (it dropped or moved
   something the previous code read), the previous code will fail against the
   current schema. Do not redeploy it; roll forward instead.

**Owner to confirm:** how many previous deployments Railway keeps available to
redeploy on the production plan.

## Migration failed or stalled

Migrations set `lock_timeout = 10s` on connect (`migrations/env.py`,
decision 238). A migration that waits on a lock **fails** after ten seconds
rather than queueing every live query behind it.

1. Read the Migrate run log. It prints `alembic current` before and after
   (`.github/workflows/migrate.yml`), so the log shows where it stopped.
2. `migrations/env.py` runs the whole upgrade inside one
   `context.begin_transaction()`, so a failure rolls the run back. The
   exception is a step inside an `autocommit_block` (used for `CONCURRENTLY`
   index builds, e.g. `20260812_1600_m4_sessions_mentor_window_index_a1c7e46b02d9.py`):
   it commits on its own, and a failed concurrent build can leave an `INVALID`
   index behind. Check for one before re-running.
3. If the error is a lock timeout, re-dispatch **Migrate** with the same target.
   Do not raise the timeout.
4. If it is any other error, it is a bug in the migration: fix it on a branch,
   merge, and dispatch again.

## Schema rollback: last resort

Every migration ships a `downgrade()`, and CI runs `upgrade head` →
`downgrade base` → `upgrade head` on a fresh database
(`test_upgrade_downgrade_upgrade_is_clean`, `tests/integration/test_migrations.py`).
That proves the **shape** comes back. It does not prove the **data** does.

**Downgrades of contract steps lose data.** Two examples from the chain:

| Migration | Its `downgrade()` says |
|---|---|
| `20260816_1100_m4_primary_offering_contract_a3c81f7e5b24.py` | "Restores the shape and **cannot restore the values.**" Every mentor lands on the column defaults |
| `20260816_2000_m4_booking_notice_floor_f1b6a92c7d4e.py` | "Restores the shape and **cannot restore the values.**" |

The db-migration skill says the same: "The real rollback plan for a destructive
migration is usually 'roll forward with a fix' plus a verified backup, not
`alembic downgrade`" (`.claude/skills/db-migration/SKILL.md`, Rollback).

If a downgrade is still the right call:

1. Read the target migration's `downgrade()` docstring. If it says values are
   not restored, take a backup first ([`backup-restore.md`](backup-restore.md)).
2. Roll the app back first, to the release that matches the revision you are
   going to.
3. Run `uv run alembic downgrade -1` from `main` with the production
   `DATABASE_URL`. **Owner to confirm:** where this runs. Migrate only offers
   `upgrade head`, so there is no workflow for a downgrade, and running it from
   a laptop puts the production DSN on that machine.

## After any rollback

| Check | How |
|---|---|
| Ready | `GET /ready` answers `200` |
| Errors stopped | Sentry shows no new events for the release (ADR 0030) |
| A failing request | Take its `X-Request-ID` from the response and find it in the Railway logs, which are JSON with `request_id` on every line (decision 241) |
| Schema | Migrate log, or `alembic current`, matches the revision the running code expects |
| Schedules | Still pointing at a working `PUBLIC_BASE_URL`; a rollback does not touch QStash (ADR 0028 Decision 6) |

Then write down what happened and why. If the fix is not shipping today, open a
GitHub issue for it (CLAUDE.md rule 11).
