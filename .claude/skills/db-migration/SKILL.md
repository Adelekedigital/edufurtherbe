---
name: db-migration
description: Zero-downtime database schema changes using expand/contract, with Alembic. Covers rolling-deploy safety, backfills, lock hazards, index creation, and rollback. Use when adding/removing/renaming a column or table, changing a column type or nullability, adding a constraint or index, writing an Alembic migration, backfilling data, or when asked whether a schema change is safe to deploy.
---

# Database Migrations

The rule that governs everything here:

> **During a rolling deploy, old and new application code run against the same
> database at the same time.**

Every migration must be safe for *both* versions simultaneously. A schema change
that only works with the new code takes production down the moment the first old
pod handles a request — which is during every deploy, not just failed ones.

This is why "add the column and start using it" is a two-release operation, not
one. Almost every migration outage traces back to compressing it into one.

## Expand / Contract

Three phases, each a **separate release**. Never combine them.

```
Release N     EXPAND    Add the new thing. Nullable/defaulted. Old code unaffected.
                        New code dual-writes: old AND new.
              ─────────────────────────────────────────────────────────────
Release N+1   MIGRATE   Backfill existing rows in batches.
                        New code reads new, still writes both.
              ─────────────────────────────────────────────────────────────
Release N+2   CONTRACT  Stop writing old. Then drop it.
                        Only safe once no running code touches it.
```

You must be able to roll back to the previous release at every point. If a
rollback would break because the previous code cannot read the current schema,
the phase was too big.

### Worked example: renaming `email` → `contact_email`

A rename is never a rename. It is add → dual-write → backfill → switch → drop.

```python
# Release N — EXPAND. Nullable, no default rewrite, no lock.
def upgrade() -> None:
    op.add_column("users", sa.Column("contact_email", sa.String(320), nullable=True))

def downgrade() -> None:
    op.drop_column("users", "contact_email")
```

Application code in release N writes **both** columns and reads `email`.

```python
# Release N+1 — MIGRATE. Batched backfill; never a single UPDATE over the table.
def upgrade() -> None:
    conn = op.get_bind()
    while True:
        result = conn.execute(sa.text("""
            UPDATE users SET contact_email = email
            WHERE id IN (
                SELECT id FROM users
                WHERE contact_email IS NULL AND email IS NOT NULL
                LIMIT 5000
            )
        """))
        if result.rowcount == 0:
            break
```

Release N+1 code reads `contact_email`, falls back to `email` if null, writes both.

```python
# Release N+2 — CONTRACT. Only after confirming nothing reads `email`.
def upgrade() -> None:
    op.drop_column("users", "email")

def downgrade() -> None:
    # Recreates the column but NOT the data. State this honestly — a
    # downgrade that silently loses data is worse than one that refuses.
    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
```

Before contracting, **verify** nothing reads the old column: grep the codebase,
and check query logs or `pg_stat_statements` for live references. "I think we
removed it" is not verification.

## Operations by risk

### Safe on a live table

- Add a **nullable** column with no default
- Add a column **with** a default — PostgreSQL 11+ only, where it is metadata-only
- Add an index **`CONCURRENTLY`**
- Drop an index `CONCURRENTLY`
- Rename an index
- Add a `CHECK` constraint as `NOT VALID`, then `VALIDATE` separately

### Dangerous — takes a lock, rewrites, or breaks old code

| Operation | Hazard | Do instead |
|---|---|---|
| `ADD COLUMN ... NOT NULL DEFAULT` (PG < 11) | Full table rewrite under `ACCESS EXCLUSIVE` | Add nullable → backfill → set `NOT NULL` via `NOT VALID` + `VALIDATE` |
| `ALTER COLUMN ... TYPE` | Table rewrite, blocks reads and writes | New column → dual-write → backfill → switch → drop |
| `CREATE INDEX` (non-concurrent) | Blocks writes for the build duration | `CREATE INDEX CONCURRENTLY` |
| `ADD FOREIGN KEY` | `ACCESS EXCLUSIVE` on both tables while validating | Add `NOT VALID`, then `VALIDATE CONSTRAINT` |
| `DROP COLUMN` while old code selects it | Old pods error on every query | Contract phase only, after verifying no readers |
| `RENAME COLUMN` | Old code breaks instantly | Never rename in place — use expand/contract |
| Adding `NOT NULL` to an existing column | Full scan under lock | `CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE` → `SET NOT NULL` |

### The lock queue trap

In PostgreSQL, a blocked `ALTER TABLE` **also blocks every query queued behind
it**, including plain `SELECT`s. A migration waiting on a long-running
transaction does not just stall itself — it stalls the table. Always set a short
lock timeout so the migration fails fast instead of cascading into an outage:

```python
def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.execute("SET statement_timeout = '30s'")
    op.add_column("users", sa.Column("contact_email", sa.String(320), nullable=True))
```

Retry the migration rather than letting it hold the queue open.

### Concurrent index creation in Alembic

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction, and Alembic wraps
migrations in one:

```python
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_orders_customer_id", "orders", ["customer_id"], postgresql_concurrently=True
        )
```

A `CONCURRENTLY` build that fails leaves an **`INVALID` index** behind. It is not
used by the planner but still costs write overhead. Check for these after any
failed index migration:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

## Backfills

- **Batch, always.** A single `UPDATE` over millions of rows holds one
  transaction, bloats WAL, blocks vacuum, and cannot be interrupted safely.
- Batch size 1,000–10,000, with a short sleep between batches on a busy table.
- Make it **resumable and idempotent** — filter on `WHERE new_col IS NULL` so a
  re-run picks up where it stopped.
- For very large tables, run the backfill as a **separate job**, not inside the
  migration. Migrations should be fast; a 40-minute migration blocks the deploy
  pipeline and often gets killed halfway by a deployment timeout.
- Log progress. A silent backfill is indistinguishable from a hung one.

## Rollback

Every migration needs a `downgrade()` that has actually been run at least once
locally. An untested `downgrade` is decoration.

Be honest about irreversibility. If a downgrade cannot restore data, say so in
the docstring rather than silently recreating an empty column:

```python
def downgrade() -> None:
    """Recreates the column; data is NOT recovered — restore from backup."""
```

The real rollback plan for a destructive migration is usually "roll forward with
a fix" plus a verified backup, not `alembic downgrade`. State which one applies
in the PR body.

## Alembic conventions

- **One logical change per migration.** Mixing a rename and an index makes
  partial failure unrecoverable.
- **Always review autogenerated migrations.** Autogenerate misses server
  defaults, check constraints, enum changes, and index renames, and it happily
  produces destructive drops from a stale model.
- Set a **naming convention** in metadata so constraint names are deterministic
  across environments — otherwise downgrades fail on names that differ per
  database.

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
```

- Migrations run as a **separate step** in deployment, before the new code rolls
  out — never on application startup. Startup migrations race across replicas.
- Migration DB credentials need DDL rights; the application's runtime user should
  not have them.

### Name every constraint explicitly

Put a `naming_convention` on `Base.metadata` — and **still name PK, UNIQUE, and
index constraints explicitly in hand-written migrations.**

An unnamed constraint gets a name that depends on the *Alembic version running at
the time*: older versions emitted the database default (`<table>_pkey`), current
ones apply your `naming_convention` (`pk_<table>`). The same migration file
therefore produces different schemas at different times.

The consequence is worse than untidy names. A migration that hard-codes existing
names in order to rename them cannot run against a fresh database — so the chain
becomes un-runnable from empty and **no new environment can be provisioned**.
This stays invisible until someone tries.

**Guard every rename with an existence check, in both directions:**

```python
op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'orders_pkey') THEN
        ALTER TABLE orders RENAME CONSTRAINT orders_pkey TO pk_orders;
      END IF;
    END $$;
""")
```

An unguarded `downgrade` fails a CI reversibility check — which is the point of
having one.

### What `alembic check` does not see

`alembic check` is autogenerate comparison. It sees tables, columns, types, and
regular indexes. It is **blind to CHECK constraints, partial indexes, and any
model never imported into `env.py`** — which is exactly where hand-written
migration bugs live.

So constraint- and index-name parity stays a **manual** recreate-and-diff against
the live schema. Automating migrations-apply-from-empty narrows that manual
check; it does not retire it. Green CI is not proof a migration is fully correct.

## Testing migrations

```python
async def test_migration_is_reversible(alembic_engine) -> None:
    """upgrade -> downgrade -> upgrade must be clean. Catches downgrades that
    were never run, which is most of them."""
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")


def test_no_multiple_heads() -> None:
    """Two migrations branching from one parent — a merge artifact that breaks
    deploys with a confusing error."""
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
```

Run migrations against a **real** database (testcontainers), never SQLite. SQLite
silently accepts DDL that PostgreSQL rejects, so the test proves nothing about
production.

Test the chain **from empty**, not just from your current revision — an
environment that cannot be provisioned from scratch is a broken chain nobody has
noticed yet.

> **Never run `alembic downgrade` locally** if your `env.py` reads the same
> connection string your development environment uses. Reversibility testing
> belongs in CI, against an ephemeral database. Column semantics and the
> `created_at == updated_at` trap live in `persistence-patterns`.

## Pre-merge checklist

- [ ] Safe for old **and** new code running simultaneously
- [ ] Expand / migrate / contract split across separate releases
- [ ] `lock_timeout` and `statement_timeout` set on any `ALTER`
- [ ] Indexes created `CONCURRENTLY` inside an `autocommit_block`
- [ ] Backfill batched, resumable, and outside the migration if large
- [ ] `downgrade()` written and actually executed locally
- [ ] Irreversibility documented where it exists
- [ ] Single head; one logical change
- [ ] Autogenerated SQL reviewed line by line
- [ ] Tested against real PostgreSQL, not SQLite
- [ ] PR states the rollback plan: downgrade, or roll-forward + backup
