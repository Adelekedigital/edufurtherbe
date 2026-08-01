---
name: persistence-patterns
description: SQLAlchemy and ORM-layer standards for async Python services — the Pydantic/ORM boundary, internal vs external identifiers, choke-point accessors, async relationship loading, flush and transaction rules, table shape, and timestamp column semantics. Use when writing a model or service function, adding a relationship, choosing an identifier to pass between layers, debugging MissingGreenlet or a query returning None, or deciding what belongs in a schema versus a column.
---

# Persistence Patterns

The rules here exist because each one has a failure mode that is **silent** — no
exception, no test failure, just wrong data or an empty result.

## The Pydantic ↔ ORM boundary

Pydantic is the API boundary. The ORM is the database. They never meet in a
signature.

```
Route                Service              Database
─────                ───────              ────────
Pydantic in    →     ORM model     →      SQLAlchemy
Pydantic out   ←     ORM model     ←      SQLAlchemy
```

- Routes accept `XCreate` / `XUpdate`, return `XRead`.
- Services accept primitives or ORM objects, return ORM objects, or raise.
- Convert at the route edge: `XRead.model_validate(obj, from_attributes=True)`.
- **Never** annotate a service parameter or return with a Pydantic class.
- **Never** annotate a route response with an ORM class — that serializes your
  schema, including columns you did not mean to expose.

### Derived values are computed, not stored

When a value is a pure function of data the schema already carries, compute it at
the boundary with a Pydantic `computed_field` rather than storing a column.

A stored copy needs a migration and a backfill, and can drift from the function
that defines it. A computed one cannot drift. It also renders as read-only in the
generated schema and never appears in a request body.

The computed property must read **only schema fields**, never an ORM object — if
it touches the ORM, the boundary above is broken.

Store the value instead only when it depends on something mutable, or must be
queried or indexed in SQL.

## Identifiers: translate once, at the edge

**External identifiers are translated exactly once, at the boundary. Everything
deeper takes internal ids.**

A domain service signature takes *your* id (`app_user_id: UUID`), never an
identity provider's (`auth_id`), never a vendor's. Translate at the edge — a
resolver function for writes, a JOIN for reads that already query.

This is not tidiness. It prevents a specific and nasty bug class:

> Comparing a session's **external** id against a column holding an **internal**
> one never matches. It raises nothing — both are UUIDs — and it fails **closed**,
> so it surfaces as a 403. You will debug it as a permissions problem, in the
> permissions code, where the bug is not.

The same shape appears with any two id spaces that share a type. Type aliases
(`AppUserId = NewType("AppUserId", UUID)`) make the mismatch visible to the type
checker instead of at runtime.

### Name FK columns after what they reference

`<referenced_entity>_id` — `app_user_id`, `connected_account_id`. Not an
abbreviation, not a role word.

`post_id` becomes ambiguous the moment a second post-ish entity exists. A name
like `owner_user_id` can actively lie — claiming to hold a user id while holding
an auth id. Use a role prefix only when the role is load-bearing *and* the plain
name would be ambiguous: `created_by_app_user_id` alongside `app_user_id`.

## Choke-point accessors

**When reading a table carries an invariant — an ownership check, or a lookup key
that is easy to get wrong — put it behind one function and never read it raw at
call sites.**

```python
async def get_business(db, *, business_id: UUID, owner_id: UUID) -> Business:
    """The only way to read a Business. Enforces ownership; raises NotFound."""
```

This is not style. Consider a table originally keyed by `business_id`, so
`db.get(ContextProfile, business_id)` was correct. A surrogate `id` is added
later. That call now targets the wrong column — and because both are UUIDs, it
**raises nothing and returns `None`**. Downstream code silently loses context and
produces degraded output with no error anywhere.

An accessor means the wrong call never gets written.

> **Prefer designing a trap out over documenting it.** A rule in a doc is a rule
> someone has to remember. A function signature is a rule the code enforces.

## Async relationship loading

Under async SQLAlchemy, the default lazy load is a landmine: touching an unloaded
collection emits IO from sync context and raises `MissingGreenlet` — usually
inside serialization, so **the traceback blames your schema rather than your
query**.

Three rules, and the second is the one people miss:

1. **Declare `lazy="selectin"`** so a normal query eager-loads the collection.
2. **That is not sufficient.** `db.get()` can return an **identity-map hit
   without issuing a query**, so the loader strategy never runs. Any fetch whose
   result gets serialized must use `select(...).options(selectinload(...))`, not
   `db.get()`.
3. **A freshly constructed object has an uninitialized collection.** If you build
   a row and serialize it in the same request without re-querying, pass the
   collection explicitly (`targets=[]`) to mark it loaded.

Set `expire_on_commit=False` on the session factory to prevent a fourth variant,
where committing expires attributes and the next access re-queries.

## Flush and transactions

- `await db.flush()` after `db.add(obj)` **only** when you need the
  DB-generated id or a server default before commit — the object is about to be
  validated into a schema, or its id is a foreign key in a later statement in the
  same function. Otherwise, no flush.
- **The service owns the commit.** The session dependency rolls back on
  exception; it does not commit. Background work opens its own session.
- Wrap multi-statement operations in an explicit transaction. Never rely on
  autocommit for a business operation.

## Table shape

- **Every table carries its own surrogate `id` primary key** — including 1:1
  extension tables. The shared-primary-key pattern is a legitimate alternative,
  but uniformity wins: every table looks the same and nothing must be remembered
  per-table.
- **A 1:1 therefore needs an explicit `UNIQUE(<fk>_id)`.** The shared PK gave you
  that for free; a surrogate PK does not. Ship without it and the database
  silently permits two rows, turning every "one row per X" docstring into a claim
  nothing enforces.
- **Do not add `index=True` to a column that already has `UNIQUE`.** The
  constraint creates an index; a second one is pure write overhead.

### Timestamps are row metadata, not domain semantics

Every table gets `created_at` and `updated_at` from one shared mixin. An
append-only ledger still gets `updated_at` even though it can never move. A table
with a meaningful domain timestamp (`sent_at`, `received_at`) keeps *that* as the
domain column and still carries the metadata pair.

**Enforce this with a test that inspects every model, not with the mixin.** A
mixin can be forgotten on a new model; a red suite cannot.

Three traps:

- **`onupdate` is ORM-side, not a database trigger.** It fires on a flush of a
  dirty object or a Core `update()`. A raw SQL `UPDATE` will not move it.
- **`now()` is transaction-scoped in PostgreSQL.** It returns the transaction's
  start time and does not advance within it — so a test asserting "the timestamp
  moved" cannot pass inside a single-transaction test harness. Age the row
  backwards first, then assert the mutation dragged it forward.
- **Never compare `created_at == updated_at` to mean "never edited."** Two
  independent clock reads at INSERT. On nanosecond-resolution hosts (Linux, your
  production) they always differ, so the check is always false. On coarse-clock
  hosts (Windows, some developer machines) they are equal, so it is always true.
  The idiom is **broken in production and green in CI** — the worst combination:
  it reviews as obviously correct and never fires where it matters. If a row
  needs a "never edited" signal, stamp both columns from **one** timestamp call
  at insert, and test it by patching the clock to a sentinel so the assertion
  cannot pass by coincidence.

## Constraint naming

Put a canonical `naming_convention` on `Base.metadata` so models and the live
database agree.

**Then still name PK, UNIQUE, and index constraints explicitly in hand-written
migrations.** An unnamed constraint produces a name that depends on the *Alembic
version in use at the time* — older versions emitted the database default
(`<table>_pkey`), current ones apply your `naming_convention` (`pk_<table>`). The
same migration file therefore yields different schemas at different times.

That is not theoretical: a migration that hard-codes old names to rename them
cannot be applied to a fresh database, which makes the whole chain un-runnable
from empty and means **no new environment can be provisioned**. Guard every
rename with an existence check, in **both** directions — an unguarded downgrade
fails a reversibility check.

A second foreign key to the same referred table always needs an explicit name.

## Error handling

Services raise domain exceptions (`NotFound`, `Forbidden`, `Conflict`,
`ValidationError`). Routes never raise raw HTTP exceptions; one handler maps
domain exceptions to status codes and the error envelope. See `api-designer`
for the envelope shape.

## Checklist

- [ ] No Pydantic class in a service signature; no ORM class in a route response
- [ ] Derived values computed at the boundary, not stored, unless queried in SQL
- [ ] External ids translated at the edge; internal code takes internal ids
- [ ] FK columns named `<referenced_entity>_id`
- [ ] Invariant-carrying reads behind a single accessor
- [ ] Relationships `lazy="selectin"` **and** fetched with `selectinload`, not `db.get()`
- [ ] `flush()` only where an id or server default is needed before commit
- [ ] Service owns the commit
- [ ] 1:1 tables carry an explicit `UNIQUE` on the FK; no redundant index
- [ ] PK/UNIQUE/index named explicitly in migrations; renames guarded both ways
- [ ] No `created_at == updated_at` comparison anywhere
