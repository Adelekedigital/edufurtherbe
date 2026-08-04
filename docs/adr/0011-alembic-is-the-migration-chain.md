# 11. Alembic is the migration chain; the package DDL is its specification

Date: 2026-08-03

## Status

Proposed

> Numbered 0011 rather than 0008. ADR 0007 allocates 0008, 0009 and 0010 to the
> three conflicts it deferred — institutions, first-login authentication, and
> message thread scope. Those numbers are named in an accepted record, so they are
> spoken for even though the files do not exist yet, and a reader following that
> table to 0008 should not land on this.

## Context

ADR 0007 adopted `docs/edufurther-migration/` as the canonical target data model
and left an open question aimed directly at this work:

> **Whether the package's DDL becomes the migration chain or a specification for
> it.** Ten files that run in order against an empty database is not the same
> artefact as an Alembic chain that must also run forward from a populated one.
> Decide before M0 is written.

M0 is being written, so it is decided here.

**The package is ten `.sql` files** applied with `psql` in numeric order. Each is
a single transaction and, in the package's own words, "re-runnable against an
empty database." There is no revision graph, no downgrade, and no record in any
environment of which files have been applied.

**This repository had no persistence layer at all** before the change this record
accompanies: no SQLAlchemy, no Alembic, no driver, and no database setting on
`Settings`. There is no incumbent to displace, which means the cost of choosing is
the cost of the first implementation and nothing more.

**Two existing standards assume Alembic and models.** The `db-migration` skill is
written around revisions, downgrades, a naming convention, and a chain applied
from empty in CI. `persistence-patterns` requires the timestamp guarantee be
enforced "with a test that inspects every model, not with the mixin" — which
cannot exist without models. `mypy` runs in strict mode, where raw SQL yields
`Row` objects and every downstream annotation becomes a guess.

## Decision

**Alembic is the chain of record. The package's DDL is a specification: read and
transcribed, never executed against any environment.**

1. **SQLAlchemy models are the source of truth for tables and columns.**
   Autogenerate produces a first draft from M1 onward, and every generated
   migration is reviewed line by line before it is committed.
2. **Everything autogenerate cannot see is hand-written**: extensions, enums,
   functions, triggers, partial and GIN indexes, `CHECK` and exclusion
   constraints. That is the majority of M0, which is why M0's migration is
   hand-written in full and autogenerate is not run for it.
3. **A `naming_convention` sits on `Base.metadata`**, and PK, UNIQUE and index
   names are still spelled out explicitly in hand-written migrations.
4. **CI applies the chain from empty** against a real PostgreSQL, and a
   reversibility test runs `upgrade → downgrade → upgrade`.
5. **Migrations run as a separate deploy step**, never on application startup,
   which races across replicas.
6. **Enums and lookup tables ship with the phase that first uses them**, not all
   at once in a foundation migration. `00_foundation.sql` declares 40 enums and 7
   lookup tables; exactly 2 of those enums are used by its own tables.

### Why not execute the package DDL

The decisive argument is that the two artefacts answer different questions. Ten
files that run against an empty database describe a destination. A migration
chain has to get a *populated* database from where it is to where it should be —
and the moment production holds a row, the `.sql` files can never be run again,
leaving no mechanism for the second change. Adopting them as the chain would mean
adopting a chain with exactly one link.

### Rejected alternatives

**Run the `.sql` files as the chain, adopt Alembic later.** Cheapest today, and it
gets a database up fastest. Rejected because "later" still means transcribing
everything, plus stamping a baseline revision against a schema nobody generated
from models — the same work, done once the cost of being wrong is higher.

**Wrap each `.sql` file verbatim in one Alembic revision.** Genuinely tempting: it
preserves the package's text exactly, gets a version table immediately, and is
much faster than modelling 66 tables. Rejected on two counts. Without models,
`alembic check` compares nothing and the every-model timestamp test cannot be
written, so two guardrails become promises. And it imports the package's mistakes
unread — transcription is what surfaced the `countries` comment that contradicts
its own foreign keys, and the `updated_at` trigger's interaction with a re-runnable
import. Copy-paste would have shipped both.

**Models only, autogenerate everything.** Rejected because autogenerate is blind
to functions, triggers, partial indexes and `CHECK` constraints, which is most of
what this schema's correctness rests on.

## Consequences

**The package and the chain will diverge, and nothing detects it.** ADR 0007
already states this — "nothing checks that the package's DDL and the Alembic
migrations written later stay in agreement, and the divergence will be silent when
it starts." This record does not fix that. What it adds is a rule: **where the
chain departs from the package, the migration that departs says so and why.** A
divergence recorded at the point of divergence is reviewable; one discovered later
by diffing two schemas is not.

**M0 is no longer gated by ADR 0008.** ADR 0007 lists institutions as blocking M0
because `institutions` is created in `00_foundation.sql`. Under point 6 above it
is created in M2, with the education tables that first reference it, so the
foundation migration creates no object that waits on an open decision. The gate
did not move because the decision was made; it moved because the table did.

**Expand/contract does not apply to a greenfield table.** It applies from the
first migration that touches a table holding data with code reading it. Applying
`CONCURRENTLY`, `NOT VALID`/`VALIDATE` and dual-write ceremony to an empty
database adds steps and risk for no benefit, and doing it out of habit would
obscure the migrations where it genuinely matters.

**Two chains of provenance now exist for the same schema**, and a reader must know
which to trust for what. The package is canonical for *what the target contains*;
the Alembic chain is canonical for *what any database actually contains*. When
they disagree, the database is right and the disagreement is a bug in one of them.

### Confirmation

- **Mechanical:** CI applies the chain to an empty PostgreSQL 17 on every pull
  request, and fails if the chain has more than one head.
- **Mechanical:** a test runs `upgrade → downgrade → upgrade`, so a downgrade that
  was never executed cannot merge.
- **Mechanical:** `REQUIRE_DB_TESTS=1` in CI turns "no database, so skip" into a
  failure, so the database tier cannot silently vanish and leave a green check.
- **Not mechanical:** nothing compares the chain against the package's DDL. Named
  in ADR 0007, unchanged by this record, and the largest gap in it.
- **Not mechanical:** `alembic check` sees tables, columns, types and regular
  indexes. It is blind to functions, triggers, partial indexes and `CHECK`
  constraints — which is precisely what M0 consists of. Green CI is evidence the
  chain applies, not that it is complete.
- **Not mechanical:** nothing prevents someone running the package's `.sql` files
  against a database directly. Enforced by review against this record.

### Open questions

- **Whether the migration role should differ from the runtime role.** The
  `db-migration` standard says the application's user should not hold DDL rights;
  Supabase provides one `postgres` role. Unresolved, and it needs an answer before
  the first deployed migration rather than before the first written one.
- **Pooling mode**, still open from ADR 0005. Transaction-mode pooling is
  incompatible with client-side prepared statements and will require `NullPool`
  and a disabled asyncpg statement cache. Not configured on a guess.
