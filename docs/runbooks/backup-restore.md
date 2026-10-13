# Runbook: backup and restore

How production data comes back after loss, and the drill that proves it can.
Read the warning first.

## Where we stand

ADR 0005 (`docs/adr/0005-data-platform.md`) records three things, and all three
are still true:

| ADR 0005 says | Meaning |
|---|---|
| "**Point-in-time recovery is a paid add-on.** On daily snapshots alone, a restore can lose up to a day." (Consequences) | Without PITR, the worst case is a day of bookings, credits and sign-ups |
| "nothing verifies that a restore actually works … A restore that has never been performed is an assumption." (Confirmation) | **No restore has ever been tested** |
| "**Which Supabase plan**, decided against the point-in-time-recovery need during the migration window rather than against monthly cost." (Open questions) | The plan, and so the backup tier, is undecided |

**Owner to confirm**, before go-live:

1. The Supabase plan for the production project, and whether PITR is enabled.
2. The backup retention that plan gives.
3. Who may trigger a restore, and that they have dashboard access.

Until a drill has passed, treat every number on this page as unproven.

## What a restore puts at risk

Data written after the restore point is gone. Two tables need a thought each:

- **`outbox_events`**: a message sent after the restore point comes back as
  `pending` and will be drained again. The Loops adapter sends the outbox row's
  id as the `Idempotency-Key`, so a replay "replays the provider's answer rather
  than sending a second copy" (`LoopsNotifier`,
  `src/app/infra/clients/notifications.py`). **Owner to confirm:** how long
  Loops honours an idempotency key; past that window a replay is a second email.
- **`credit_lots` / `credit_transactions`**: the ledger is append-only (ADR
  0027). A restore rolls back grants and spends together, so balances stay
  consistent with the restored sessions, but the 1st-of-month grant or an
  expiry may need re-running if the restore point is before it (below).

## Restore drill (non-production)

Run against a **scratch** Supabase project or a local Postgres, never against
production. ADR 0003 asks for rehearsal on the same code path, which is why the
drill uses the real Migrate workflow where it can (ADR 0017 Decision 3).

1. **Take the backup.**
   - Supabase backup or PITR: from the production project's dashboard, restore
     into a new project. **Owner to confirm:** the plan supports restoring into
     a different project, and the steps.
   - Logical backup: `pg_dump --format=custom --no-owner` of the production
     database to a file kept outside the repository (the repo ignores `data/`,
     `exports/` and `*.csv`, but it holds PII, so it must not live in the
     checkout at all; README, Configuration). **Owner to confirm:** where dumps
     are stored, encrypted, and for how long.
2. **Restore it.** For a dump: `pg_restore --no-owner --dbname=<scratch DSN> <file>`.
   Note the time it took.
3. **Check the schema revision.** With `DATABASE_URL` pointing at the scratch
   copy, run `uv run alembic current` and `uv run alembic heads`. They must
   match. If `current` is behind, the backup predates a migration: run
   `uv run alembic upgrade head` (or dispatch Migrate against a scratch
   environment) and record that it was needed.
4. **Check the credit ledger.** For every lot, granted plus every non-grant
   movement equals what is left. This is the invariant
   `tests/integration/test_credit_expiry.py` (`reconciles`) asserts; run it
   over all lots and expect **zero rows**:

   ```sql
   SELECT l.id
   FROM credit_lots l
   LEFT JOIN credit_transactions t ON t.credit_lot_id = l.id
   GROUP BY l.id, l.quantity_granted, l.quantity_remaining
   HAVING l.quantity_granted
          + COALESCE(SUM(t.delta) FILTER (WHERE t.reason <> 'grant'), 0)
          <> l.quantity_remaining;
   ```

5. **Check the outbox.** Count `outbox_events` by `status` (`pending`, `sent`,
   `failed`, `skipped`; the table's CHECK in
   `src/app/infra/db/models/platform.py`). A large `pending` count is what a
   real restore would drain on its next settlement run.
6. **Boot the app on it.** Start the API against the scratch copy and hit
   `GET /ready` and one signed-in
   request. Do **not** point a scratch app at production Loops, Daily or
   QStash.

## Restoring production for real

1. Disable the production schedules first so nothing drains or grants mid-restore:
   set `QSTASH_SCHEDULE_OVERRIDES` to `{"<job>":{"enabled":false}}` for each job
   and reconcile production, dry run then apply (`go-live.md` step 5).
2. Put the app in a state that takes no writes. **Owner to confirm:** how (scale
   Railway to zero, or a maintenance page on the front end). The repo has no
   maintenance mode.
3. Restore, then run steps 3 to 5 of the drill against production.
4. Redeploy the app version that matches `alembic current` (`rollback.md`).
5. Re-enable the schedules: restore the go-live overrides and reconcile, dry run
   then apply. Reconcile is what makes QStash match the repository again
   (ADR 0028 Decision 5).
6. If the restore point is before a 1st-of-month grant or expiry, dispatch the
   recovery workflow for it (`monthly-credits.yml`, `expire-credits.yml`).
   Both are safe to repeat: the monthly grant is guarded by
   `uq_credit_lots_one_monthly_grant_per_period` with `ON CONFLICT DO NOTHING`
   (`src/app/infra/db/credit_grants.py`), and the expiry sweep skips lots
   already at zero (`src/app/infra/db/credit_expiry.py`).

## Drill cadence

Run the drill **quarterly**, and after any change to the Supabase plan. Record
each run as a GitHub issue comment or a dated line in this file:

| Date | Backup type | Restore time | Revision matched | Ledger rows (expect 0) | By |
|---|---|---|---|---|---|
| | | | | | |

The first passed drill closes ADR 0005's "a restore that has never been
performed is an assumption".
