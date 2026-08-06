# 14. Every table has a generated surrogate primary key, without exception

Date: 2026-08-05

## Status

Accepted. **Reverses `project-conventions` settled decision #22**, which
authorised natural keys on ISO lookup tables, and the M1 extension of the same
reasoning to 1:1 and junction tables.

## Context

Tier-1 `persistence-patterns` requires every table to carry its own surrogate
`id`. That rule was overridden twice in this repository, each time deliberately
and each time with an argument that reads well on its own:

**Decision #22 — ISO lookups.** `countries.code`, `languages.code_639_3`. The
argument: the ISO code is externally standardised, stable, and *already the value
every foreign key would store*, so a surrogate means each foreign key holds a
UUID that must be joined back to recover the code the caller already had.

**M1 — 1:1 extensions and junctions.** `user_profiles.user_id`,
`user_onboarding.user_id`, `user_languages (user_id, language_code)`. The
argument, taken from the migration package's own principles: a surrogate on a
strictly-1:1 extension gives one person two identifiers, and the legacy system's
`mentor_profile_id`-versus-`user_id` confusion is what that produces.

Neither argument is wrong on its own terms. The problem is what they produced
together: **four different primary-key shapes across ten tables** — a generated
uuid, a caller-supplied uuid, a natural ISO code, and a composite pair — in a
schema that is one phase into a six-phase migration with fifty-six tables still
to come.

**No gate ever objected.** The rule existed in a tier-1 skill; both overrides
were written into tier-2 rows; `ruff`, `mypy`, `check_layers`, `alembic check`
and the whole suite stayed green through both. The overrides were not sneaked in
— they were argued, recorded, reviewed, and merged. That is precisely what makes
this worth a record: **a convention enforced only by prose is re-decided by
whoever reads it next**, and each re-decision is locally defensible.

## Decision

**Every table has `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`. There are no
exceptions, and a future exception requires an ADR superseding this one.**

1. **Natural keys become `UNIQUE` constraints.** `countries.code`,
   `countries.code_alpha3` and `languages.code_639_3` keep `NOT NULL` and
   uniqueness. Lookup by code is unchanged; what changes is that a *referencing*
   row stores the id.
2. **Composite keys become `UNIQUE` constraints.**
   `user_languages (user_id, language_id)` is a unique index rather than a
   primary key. **This is the part most easily lost** — the invariant was free
   under the old key and must now be declared.
3. **1:1 extensions get `UNIQUE (user_id)`.** Same reasoning: `user_profiles` and
   `user_onboarding` no longer get one-row-per-user from the primary key.
4. **Foreign keys reference `id`.** `origin_country_code char(2)` became
   `origin_country_id uuid`; `user_languages.language_code` became `language_id`.
   This is a departure from `docs/edufurther-migration/`, which specifies
   `char(2) REFERENCES countries(code)`, and the M1 migration states it.
5. **Reference-table ids are not stable across environments.** Seeds omit `id`
   and let the default generate one, so `NG` has a different id in every
   database. Nothing may depend on a literal reference id; resolve by code.

### Rejected alternatives

**Keep #22 for the ISO tables only.** The narrowest change, and the one initially
recommended: those two tables are externally governed, and the code genuinely is
what a caller holds. Rejected because a rule with exceptions costs every future
reader the memory of the exceptions, and "every table has an id except these two"
is a tax paid on all fifty-six remaining tables to save a join on two. At this
data volume the join is free; the ambiguity is not.

**Surrogate `id` on the lookups, but foreign keys still referencing `code`.**
PostgreSQL permits a foreign key against any unique column, so this would satisfy
"every table has an id" while keeping readable foreign keys. Rejected as the worst
of both: the `id` column would exist and never be read, which is consistency on
paper and not in use.

**Deterministic reference ids, derived from the code with `uuid5`.** Would make
`NG` the same id in every environment, so rows could be copied between them.
Rejected on principle — a surrogate computed from the natural key moves whenever
ISO retires or reassigns a code (`CS` split into `RS`/`ME`; `AN` retired in 2010),
which is exactly the volatility a surrogate exists to absorb. Committing literal
UUIDs instead would add ~286 KB to a 236 KB seed migration, past pre-commit's
500 KB ceiling — a limit this repository has already hit once on this same file.

## Consequences

**Consistency is now the property being optimised**, explicitly and at a cost.
Reading a `user_profiles` row shows `origin_country_id` rather than `NG`, and
answering "which country" takes a join. That is accepted.

**The M1c transform gains a resolution step**: country and language *names* →
ISO codes → ids. One extra lookup against a table already loaded in M0.

**The migration package is departed from on foreign-key types.** Its DDL uses
`char(2) REFERENCES countries(code)` throughout, and M2–M6 will each need the
same substitution. ADR 0011 requires each migration to state its departures.

**Decision #22's reasoning is preserved rather than deleted**, in that record and
in this one. It was a good argument; it lost to a different consideration. A
future reader who rediscovers it should find the counter-argument attached.

**M0's shipped migration was amended in place** rather than corrected by a
follow-up `ALTER`. Nothing is deployed anywhere, and a chain that produces the
right schema from empty beats one carrying a wrong-then-corrected pair through
every future environment. This is a **one-time exception** to "never edit a
merged migration", justified by the phase, and it stops being available the
moment any environment holds a row.

### Confirmation

- **Mechanical, and the reason this record exists:**
  `tests/integration/test_schema_parity.py::test_every_table_has_a_generated_surrogate_primary_key`
  walks the live schema rather than a maintained list, and asserts three separate
  failure modes — a composite key, a key not named `id`, and an `id` the caller
  must supply. Each was proved by reintroducing it: every defect fails that test
  and nothing else. A new table cannot opt out by being forgotten.
- **Mechanical:** `test_the_natural_keys_survive_as_unique_constraints` asserts
  that `countries.code`, `languages.code_639_3`, both 1:1 `user_id` columns and
  `(user_id, language_id)` remain unique. Without it, demoting a natural key
  silently permits duplicates — the specific regression this decision creates.
- **Mechanical:** `tests/unit/test_models.py` asserts the same rule against the
  model registry, which is the direction `alembic check` cannot see.
- **Not mechanical:** nothing prevents a *new* table being added with a natural
  foreign-key column instead of an id reference — the rule governs primary keys,
  not the shape of references. Review against this record.

### Open questions

- **Whether reference tables should eventually expose a stable public
  identifier** — the ISO code already serves that purpose for APIs, so this is
  only a question if a lookup ever becomes a managed resource with its own
  endpoints.
