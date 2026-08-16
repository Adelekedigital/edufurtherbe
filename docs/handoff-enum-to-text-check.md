# Handoff — converting PostgreSQL enums to `text` + `CHECK`

**Status:** not started. Scoped and audited, not designed.
**Written:** 2026-08-15, after `LEFT_EARLY` forced a decision that a droppable
value would have made trivial.
**The rule is settled decision #100**; this is the migration plan behind it.

**One thing has moved since this was written.** `d5a83b17c9e4` added
`session_type_booking_configs.requires_booking_confirmation` — a boolean, not an
enum — and `trg_refuse_retiring_a_primary_offering`, the schema's first
business-rule trigger. Neither changes the scope below, but the column census in
"What to check before starting" is the thing to re-run rather than trust.

---

## Why

PostgreSQL cannot remove an enum value. Verified against this project's own
database rather than recalled:

```
ALTER TYPE zz_probe DROP VALUE 'b';
ERROR:  dropping an enum value is not implemented
```

`ADD VALUE` is one line; `DROP VALUE` never comes. So every label added
speculatively is permanent, and this schema has eight such labels already —
each documented in the codebase as having no producer:

| Enum | Value | Recorded reason |
|---|---|---|
| `attendance_status` | `left_early` | "no legacy field at all… first written by the product" |
| `meeting_provider` | `zoom` | "has no legacy source and must never be invented here" |
| `availability_exception_type` | `override` | "ships with no source data" |
| `auth_provider` | `linkedin` | dev export splits Email 37 / Google 6 / **LinkedIn 0** |
| `session_role` | `observer` | group sessions, unbuilt |
| `verification_status` | `pending`, `verified`, `rejected` | nothing verifies an award |

The target is **`text` + `CHECK` in the database, `StrEnum` at the Pydantic
boundary**. That keeps the database guarantee against *every* writer while making
the value set editable — a `CHECK` is dropped and re-added in one statement.

**The database guarantee is not optional here, and this is the part to not talk
yourself out of.** The API is one of three writers. `infra/etl/*.py` and
`scripts/load_*.py` write these columns with hand-written SQL and never construct
a Pydantic model. Enforcing only at the API entry point would leave the path that
has produced every data surprise this milestone completely unguarded.

## The precedent to copy

`institutions.source` already does exactly this, and its model records why:

```sql
CHECK (source = ANY (ARRAY['hipolabs'::text, 'manual'::text, 'ror'::text]))
```

`mentor_status_events.reason` is the other half of the pattern, arrived at by
accident: the M2 transform sets a `UnlistedReason` Python enum and the loader
writes `.value` into a plain `text` column. The concept is live, the Python enum
is used, and the `unlisted_reason` **PostgreSQL type is attached to no column at
all** — it is the one type that can be dropped today for free.

---

## Scope, measured

**17 enum types across 21 columns in 15 tables.** Plus `unlisted_reason`, which
has zero columns.

*Counted from the live schema on 2026-08-16, after D88's contract step dropped*
*`mentor_profiles.default_meeting_venue`. It read 22 columns before that, and*
*the figures below moved with it — re-measure rather than adjust, because this*
*inventory is a plan somebody executes.*

**This is an inventory of the present, and it cannot warn you about the future.**
A count taken from the live schema necessarily omits types that do not exist yet
— and the migration package's canonical DDL still declares several, because it
predates settled decision #100. `calendar_connections` is the known case:
`00_foundation.sql` declares `calendar_provider` and `connection_status`, that
table is deferred, and building it verbatim would add two types to the very list
below while every gate stayed green (see ADR 0012, *For the phase that builds
`calendar_connections`*). Converting 21 columns and then accepting 2 new ones is
a net loss. **#100 governs new vocabularies as well as old ones.**

| Table | Enum columns |
|---|---|
| `session_events` | 4 — `actor_type`, `from_status`, `to_status`, `reason_code` |
| `mentor_profiles` | 2 — `approval_status`, `listing_status` |
| `sessions` | 2 — `status`, `meeting_provider` |
| `session_participants` | 2 — `role`, `attendance_status` |
| 11 others | 1 each |

Types used by more than one column — these are the ones where a half-finished
conversion leaves the schema inconsistent:

- **`session_status`** — `sessions.status`, `session_events.from_status`, `session_events.to_status`
- **`meeting_provider`** — `session_type_booking_configs.meeting_venue`, `sessions.meeting_provider`
- **`lookup_status`** — `institutions.status`, `scholarship_programs.status`

---

## What will bite

### 1. Seven indexes have an enum **in their predicate**

A partial index whose `WHERE` names an enum literal cannot survive its column
changing type. Each must be dropped before the `ALTER` and recreated after.

```
ix_institutions_pending           WHERE status = 'pending_review'::lookup_status
ix_scholarship_programs_pending   WHERE status = 'pending_review'::lookup_status
ix_session_participants_one_mentor  UNIQUE, WHERE role = 'mentor'::session_role
ix_sessions_mentor_completed      WHERE status = 'completed'::session_status
ix_sessions_mentee_upcoming       WHERE status = ANY (…'pending_mentor_approval','confirmed')
ix_sessions_mentor_upcoming       WHERE status = ANY (…'pending_mentor_approval','confirmed')
sessions_no_mentor_double_booking WHERE status = ANY (…'pending_mentor_approval','confirmed')
```

Three more index the enum **columns** rather than a predicate, so they are
rebuilt by the type change but need no rewrite:
`ix_mentor_profiles_searchable`, `ix_auth_identities_provider_user`,
`ix_legal_documents_type_version`.

### 2. `sessions_no_mentor_double_booking` is the dangerous one

It is an `EXCLUDE USING gist` — the constraint that makes a mentor's overlapping
live sessions impossible — and its predicate names two enum literals. It must be
dropped and recreated.

**Do it inside one transaction**, and do not use `CONCURRENTLY` for this object:
the window where it does not exist is a window where two simultaneous bookings
both succeed. Everything else in this migration can be leisurely; this one
cannot.

Check whether `session_window()` references the status column before assuming the
function is unaffected.

### 3. Eleven columns carry a server default

Each is `DEFAULT 'x'::some_enum` and must be dropped before the type change and
re-added as a text literal, or the `ALTER` fails on a default it cannot cast.

```
institutions.status            'approved'      users.primary_role          'mentee'
scholarship_programs.status    'approved'      sessions.status             'pending_mentor_approval'
mentor_profiles.approval_status 'pending'      session_events.actor_type   'user'
mentor_profiles.listing_status 'unlisted'      session_participants.attendance_status 'pending'
user_awards.verification_status 'unverified'   user_languages.proficiency  'fluent'
```

### 4. Enum ordering is declaration order; text ordering is alphabetical

**Checked: nothing depends on it.** No query in `infra/db/` orders by an enum
column — the two `order_by` calls on enum-bearing tables sort by `created_at`.
So the conversion cannot silently reorder a list.

Re-run that check before starting; it is one grep and it is the difference
between a safe refactor and a reordered UI.

### 5. `pg_enum()` in `infra/db/types.py` exists for a reason — read it first

SQLAlchemy's `Enum` sends member **names**, not values, so the default behaviour
would have created labels `MENTEE`/`MENTOR` instead of `mentee`/`mentor`. The
helper wraps `values_callable` to prevent that, and its docstring records that
nothing fails when you get it wrong — the ORM translates both ways and every test
passes, and it surfaces later from `psql` or the ETL.

**The same trap exists in reverse.** Converting to `text` means the value written
is whatever Python hands over. `StrEnum` members are strings, so
`str(SessionStatus.COMPLETED)` is `"completed"` — but only because these are
`StrEnum` and not `Enum`. Verify every enum in `domain/enums.py` is a `StrEnum`
before relying on it, and keep writing `.value` explicitly in the ETL as it does
today.

---

## Suggested order

Not the order the tables appear in — the order that keeps each step verifiable.

1. **`unlisted_reason`** — drop the orphaned type. Zero columns, zero risk, and it
   proves the migration harness works.
2. **The single-column, single-table enums** — `admin_users`, `auth_identities`,
   `availability_exceptions`, `legal_documents`, `mentor_status_events`,
   `user_awards`, `user_languages`, `users`. Eight tables, no shared types, and
   only `ix_auth_identities_provider_user` and `ix_legal_documents_type_version`
   to rebuild.
3. **`lookup_status`** — two tables, one partial index each. First shared type.
4. **`meeting_provider`** — three columns, no index predicates. Tests the
   multi-column case without touching the constraint.
5. **`session_status`** — last, and on its own. Three columns, five index
   predicates, the exclusion constraint, and `session_events.from_status`/
   `to_status` recording transitions that must stay readable across the change.

Each step is its own migration and its own PR. **Do not batch them**: a failure
in step 5 must not require rolling back step 2.

## Definition of done, per step

- `alembic upgrade head` → `downgrade` → `upgrade` clean
- `alembic check` reports no drift after the models change
- Every dropped index and constraint recreated **with the same name**, verified by
  comparing `pg_indexes` before and after
- A `CHECK` on every converted column naming exactly the values the `StrEnum` has
- A test that the database refuses an unknown value — the whole point of not
  moving enforcement to Pydantic alone
- Full gate green

## Already checked, so you do not have to

- **`session_window()` is unaffected.** Its signature is
  `(starts timestamptz, mins integer) RETURNS tstzrange` and its body is a single
  `tstzrange(...)` — it never reads `status`. Only the *predicate* of the
  exclusion constraint that uses it does.
- **No trigger reads an enum column.** The only triggers on enum-bearing tables
  are `trg_set_updated_at` on `sessions`, `session_participants` and
  `mentor_profiles`, and it sets a timestamp. `session_events` has none.
- **Nothing orders by an enum column** — see item 4 above.

## What to check before starting

Written now because it will not be obvious later:

- **Has booking added enum columns since this was written?** It was the next
  build, and `sessions` / `session_participants` are exactly where it would land.
  Re-run the column census before trusting the counts in this document:

  ```sql
  SELECT table_name, column_name, udt_name
  FROM information_schema.columns
  WHERE table_schema='public' AND data_type='USER-DEFINED'
  ORDER BY table_name, column_name;
  ```

- **Are all members of `domain/enums.py` still `StrEnum`?** The conversion writes
  whatever Python hands over, and a plain `Enum` would write `SessionStatus.COMPLETED`
  rather than `completed`.
- Re-run the ordering grep. One command, and it is the difference between a safe
  refactor and a silently reordered UI.

## The rule, now recorded rather than proposed

Settled decision **#100** carries this, so it governs by default and this
document is the detail behind it rather than the place the rule lives:

> **A closed set is `text` + `CHECK` in the database and a `StrEnum` at the
> Pydantic boundary. Not a PostgreSQL enum.**
>
> And in the meantime, for the enums that remain: **do not add a value until
> something writes it.** `ADD VALUE` is available whenever the producer arrives;
> `DROP VALUE` is not available at all. Every one of the eight dead labels above
> was added for a feature that had not been designed yet.
