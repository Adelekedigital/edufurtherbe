# Handoff — converting PostgreSQL enums to `text` + `CHECK`

**Status: complete.** All 8 steps shipped; **this schema has no PostgreSQL enum types left**, and `pg_enum` is deleted rather than left unused.
**Written:** 2026-08-15, after `LEFT_EARLY` forced a decision that a droppable
value would have made trivial.
**The rule is settled decision #100**; this is the migration plan behind it.

**Re-censused 2026-08-16 against the live schema at `a3c81f7e5b24`.** The counts
below held. Four other claims did not, and they are corrected in place rather
than appended, because this document is a plan somebody executes:

| Was | Is | Where |
|---|---|---|
| "No trigger reads an enum column" | `apply_mentor_status` reads and writes three | *Already checked* |
| Five steps covering the conversion | they covered 15 of 21 columns | *Suggested order* |
| Eleven defaults, ten listed | the eleventh is `session_type_booking_configs.meeting_venue` | *What will bite* §3 |
| "Three more index the enum columns" | five | *What will bite* §1 |

**And one thing the document never mentioned:** `infra/db/types.py` carries a
registry of every vocabulary, and `test_every_domain_enum_is_registered_exactly_once`
asserts it partitions `domain/enums.py` exactly. Every step below must move its
class between registries in the same commit, or the suite goes red — which is the
intended behaviour, not an obstacle.

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
each documented in the codebase as having no producer — one of which has since acquired one and is struck through below:

| Enum | Value | Recorded reason |
|---|---|---|
| ~~`attendance_status`~~ | ~~`left_early`~~ | **Off this list, 2026-08-18.** It had no producer because nothing reported a *departure* — and Daily's per-participant join and leave is exactly that producer. Removing it now would mean adding it back with the room integration, so it stays. `session_participants.left_at` is the column it writes to, and is likewise kept |
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

`mentor_status_events.reason` was described here as the other half of the
pattern. **It is not, and reading it that way produces a broken migration.**

The half that was true: the `unlisted_reason` PostgreSQL type was attached to no
column at all, so it could be dropped for free. Step 1 did exactly that.

The half that was wrong: `reason` is not a closed set and must never get a
`CHECK`. It carries free text on a decline, an `UnlistedReason` value on a
self-pause — and **free admin text on an unlisting**, because
`DeclineRequest.reason` reaches it through `set_listing` (`api/deps.py`), a
thousand characters of whatever was typed. A `CHECK` naming the four values
rejects the admin path; conditioning it on `status_type = 'unlisted'` rejects it
too, since that is the very event the free text lands on.

So `UnlistedReason` is a set of **sentinels** written and read back by equality —
`may_self_resume` compares the newest unlisting against `MENTOR_PAUSED` — not a
vocabulary the column is restricted to. It lives in `UNCONSTRAINED_ENUMS` with
that reason recorded. Constraining it needs the `reason_code` / `reason_text`
split `SessionReasonCode` already documents, which is a schema change and a
separate decision from this one.

---

## Scope, measured

**17 enum types across 21 columns in 15 tables.** ~~Plus `unlisted_reason`, which
has zero columns.~~ — dropped by `c9d4e2a71f68`; every remaining type is attached
to at least one column, so there is no second free win in this list.

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

**`unlisted_reason` is the second known case, and now the sharper one.**
`08_features_platform.sql` declares
`search_impressions_suppressed.suppression_reason unlisted_reason NOT NULL`. That
table is deferred; building it verbatim re-creates the exact type step 1 dropped,
and no gate would notice — `ENUM_TYPE_NAMES` would simply be updated to match by
whoever added it. That column is `text` + `CHECK`, and its vocabulary is a closed
set in a way `mentor_status_events.reason` is not.

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
  *(the first of those was dropped by `d9e2b74c1f36`; a mentor's choice moved to
  `mentor_conferencing_options.provider` under a narrower vocabulary. Left as
  written — this document records what the conversion covered.)*
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

**Five** more index the enum **columns** rather than a predicate, so they are
rebuilt by the type change but need no rewrite:
`ix_mentor_profiles_searchable`, `ix_auth_identities_provider_user`,
`ix_legal_documents_type_version`, `ix_admin_users_active_grant` and
`ix_session_events_reason`.

The last two were missing from this list. Both carry a `WHERE` clause —
`revoked_at IS NULL` and `reason_code IS NOT NULL` — which is presumably why they
were read as predicate indexes and skipped; neither predicate names an enum
literal, so neither needs rewriting. Check the predicate, not the presence of one.

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
session_type_booking_configs.meeting_venue     'google_meet'
```

The last line was absent while the count above said eleven — the prose was right
and the list was short. It arrived with `b7f2e4c19a05`, which made
`meeting_venue` `NOT NULL` with a default after this section was written. A count
that disagrees with its own list is the cheap warning; re-derive both from the
schema rather than trusting either.

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
today. Re-verified 2026-08-16: all 18 are.

### 6. The trigger this document said did not exist

`trg_apply_mentor_status` on `mentor_status_events` reads one enum column and
writes two more, in two different tables:

```sql
IF NEW.status_type IN ('approved', 'declined') THEN
    UPDATE mentor_profiles SET approval_status = NEW.status_type::text::approval_status ...
ELSE
    UPDATE mentor_profiles SET listing_status  = NEW.status_type::text::listing_status  ...
```

The `::text::` hop is deliberate and is recorded in the function — PostgreSQL
refuses a direct cast between two enum types.

**This is the one silent failure in the whole conversion.** A plpgsql body
carries no dependency records, so `DROP TYPE approval_status` succeeds while a
function still names it. Every migration test passes, `alembic check` reports no
drift, and the trigger dies at runtime on the next insert with *type
approval_status does not exist* — on the write path that every approval and every
unlisting goes through.

So `mentor_status_events.status_type`, `mentor_profiles.approval_status` and
`mentor_profiles.listing_status` are **one unit of work**, and the function is
rewritten in the same migration. That is step 4 below, and it is why
`mentor_status_events` is no longer grouped with the single-column tables.

---

## Suggested order

Not the order the tables appear in — the order that keeps each step verifiable.

**Eight steps, not five.** The original five covered 15 of the 21 columns:
`mentor_profiles.approval_status` and `.listing_status`, `session_events.actor_type`
and `.reason_code`, and `session_participants.role` and `.attendance_status` were
named nowhere. Steps 4, 6 and 7 are where they land. The arithmetic is stated so
the next reader can check it rather than trust it: 7+2+3+2+2+2+3 = 21.

| # | Step | Columns | What it carries |
|---|---|---|---|
| 1 | **`unlisted_reason`** — drop the orphaned type | 0 | ✅ shipped as `c9d4e2a71f68` |
| 2 | **Single-column, single-table** — `admin_users`, `auth_identities`, `availability_exceptions`, `legal_documents`, `user_awards`, `user_languages`, `users` | 7 | ✅ shipped as `d1f8a3c62b47`; all three indexes verified byte-identical before and after |
| 3 | **`lookup_status`** — first shared type | 2 | ✅ shipped as `e4b7d0c95a13`; both partial indexes dropped and recreated by name |
| 4 | **The mentor status cluster** — `mentor_status_events.status_type`, `mentor_profiles.approval_status`, `.listing_status` | 3 | ✅ shipped as `f5c3a81e6b29`; `apply_mentor_status` rewritten in the same migration, and the rewrite *removed* the `::text::` hack |
| 5 | **`meeting_provider`** — `session_type_booking_configs.meeting_venue`, `sessions.meeting_provider` | 2 | ✅ shipped as `a7d2f4b8c051`; one column nullable, which the `IN` constraint permits without a special case |
| 6 | **`session_participants`** — `role`, `attendance_status` | 2 | ✅ shipped as `b8e1c5d70f42`; unique partial index rebuilt by name, invariant re-proved by inserting a second mentor |
| 7 | **`session_events`** — `actor_type`, `reason_code` | 2 | ✅ shipped as `c2f9b6a41d83`; `ix_session_events_reason` needed no rewrite, as predicted |
| 8 | **`session_status`** — last, and on its own | 3 | ✅ shipped as `d3a6f81b52c7`; the gist `EXCLUDE` and three partial indexes rebuilt by name inside one transaction, and the double-booking guarantee re-proved against real overlapping rows |

Step 2 no longer includes `mentor_status_events`: its `status_type` is read by
`apply_mentor_status`, so converting it apart from the two `mentor_profiles`
columns splits one function's dependencies across two releases. See *What will
bite* §6.

Each step is its own migration and its own PR. **Do not batch them**: a failure
in step 8 must not require rolling back step 2.

## The front-loaded audit

Run once, on 2026-08-16 at `a3c81f7e5b24`, across all eight steps — so each PR
executes a known plan rather than rediscovering one. Re-run the cheap checks
anyway; this table is the expectation to compare them against, and a step whose
real dependencies differ from it is a stop-and-ask.

**Ruled out for every step, checked and not assumed:** no views, no materialised
views, no generated or identity columns, no row-level security policies. And
`apply_mentor_status` is the **only** function in the schema that touches an enum
column — every other hit in `pg_proc` is pgcrypto, btree_gist or pg_trgm.

**`pg_depend` is the drop-list, not the predicate text.** An index appears as a
dependency of an enum type only when its definition references that type, which
is exactly the set that cannot survive the column changing type. Reading
predicates by eye is what produced the "three more indexes" error above.

```sql
SELECT t.typname, c.relname, c.relkind
FROM pg_depend d
JOIN pg_type t ON t.oid = d.refobjid AND t.typtype = 'e'
JOIN pg_class c ON c.oid = d.objid AND d.classid = 'pg_class'::regclass
WHERE t.typnamespace = 'public'::regnamespace AND d.deptype <> 'i';
```

| Step | Columns | Must drop + recreate | Defaults | Function |
|---|---|---|---|---|
| 1 ✅ | 0 | — | 0 | — |
| 2 ✅ | 7 | — none — | 3 | — |
| 3 | 2 | `ix_institutions_pending`, `ix_scholarship_programs_pending` | 2 | — |
| 4 | 3 | — none — | 2 | **`apply_mentor_status`** |
| 5 | 2 | — none — | 1 | — |
| 6 | 2 | `ix_session_participants_one_mentor` (unique, partial) | 1 | — |
| 7 | 2 | — none — | 1 | — |
| 8 | 3 | `ix_sessions_mentor_completed`, `ix_sessions_mentee_upcoming`, `ix_sessions_mentor_upcoming`, `sessions_no_mentor_double_booking` | 1 | — |

Seven index dependencies, eleven defaults, twenty-one columns — all three
reconcile against the census. Eleven of the twelve objects are **bare indexes**
(`op.drop_index` / `op.create_index`); only `sessions_no_mentor_double_booking`
is a constraint (`contype x`), and it is step 8's.

These five index enum **columns** and need no action at all, because their
predicates name no enum literal: `ix_admin_users_active_grant`,
`ix_auth_identities_provider_user`, `ix_legal_documents_type_version` (step 2),
`ix_mentor_profiles_searchable` (step 4), `ix_session_events_reason` (step 7).

### Raw SQL that names the type — the class `pg_depend` cannot see

**Found the hard way in step 2, after the audit said the step was clean.**
`pg_depend` inventories database objects. It knows nothing about a type name
sitting inside a Python string, and this codebase has 30 of them:

```
CAST(:admin_role AS admin_role)
```

`infra/etl/*.py` writes these columns with hand-written SQL — which is the whole
reason #100 keeps the guarantee in the database — and the cast names the type the
step is about to drop. It fails at runtime with *type "admin_role" does not
exist*, on the admin-grant load path. Step 2 hit two in `src/` and two in
`tests/`; the remaining twenty-six are waiting in the steps below.

**Run this before every step, and treat it as part of the census:**

```bash
grep -rnE "CAST\([^)]*\bAS\s+(TYPE1|TYPE2)\b|::(TYPE1|TYPE2)\b" src/ tests/ scripts/
```

**Match anything inside `CAST(...)`, not a parameter or a bare word.** The first
version of this grep was `CAST\(\s*:?\w+\s+AS\s+…`, which assumed the cast
argument is `:param` or an identifier. Step 3 shipped a green local run and then
failed three tests on `CAST('pending_review' AS lookup_status)` — a **quoted
literal**, which `\w+` cannot match. Both branches must also list *every* type
the step drops; the same first version listed the step's types in the `CAST`
branch and a stale set in the `::` branch, so postfix casts were invisible too.
A census grep that silently matches less than it claims is worse than no grep.

The fix is to delete the cast, never to keep it: the parameter is already a
string and the column is now `text`. The guarantee moves to the `CHECK`, which
refuses exactly what the enum did on exactly this path.

Re-derived with the corrected pattern; the counts below are higher than the
first pass reported, which is the point.

| Step | Sites | Where |
|---|---|---|
| 3 `lookup_status` ✅ | 5 | `test_api_catalogue.py` ×2, `test_api_admin.py`, `test_api_user_attributes.py`, `test_institution_sync.py` |
| 4 `approval_status`, `listing_status`, `mentor_status_type` | 6 | `infra/etl/profiles.py` ×3, `factories.py`, `test_api_slots.py`, `test_mentor_status_log.py` |
| 5 `meeting_provider` | 3 | `infra/etl/sessions.py` ×2, `factories.py` |
| 6 `session_role`, `attendance_status` | 5 | `infra/etl/sessions.py` ×2, `test_api_mentor_stats.py` ×2, `test_sessions_schema.py` |
| 7 `actor_type` | 2 | `infra/etl/sessions.py`, `test_api_sessions.py` |
| 8 `session_status` | 12 | `infra/etl/sessions.py` ×3, `factories.py`, `test_api_mentor_stats.py`, `test_api_sessions.py` ×2, `test_api_slots.py`, `test_sessions_schema.py` ×4 |

Step 3 was scoped at two sites and was really five — the three extra were quoted
literals. Step 8's twelve are the ones to be careful with; four of them are
quoted literals too.

### Verify against a private database, not the shared one

Step 4 found the local `edufurther` database on a revision that exists in no
branch (`f1b6a92c7d4e`), with the step-2 columns back to enums — another
workstream on the same PostgreSQL had migrated it. A `git worktree` isolates the
checkout and **isolates nothing about the database**.

The suite was unaffected, and that is worth knowing rather than assuming: the
`db` tier uses `TEST_DATABASE_URL` only as a *maintenance* connection to create
disposable databases, so its own schema state never matters. Only hand-run
`alembic upgrade` verification does.

So per-step manual verification creates its own database and drops it:

```bash
docker exec edufurther-postgres psql -U edufurther -d postgres -c "CREATE DATABASE enumstepN;"
DATABASE_URL=postgresql://edufurther:edufurther@localhost:55432/enumstepN uv run alembic upgrade head
```

### A table rewrite discards statistics, and the planner notices

`ALTER COLUMN ... TYPE` rewrites the table. The rewrite also throws away its
statistics, and **the planner will not choose a partial index it can no longer
cost**. Step 5 was caught by `test_the_completed_count_uses_its_partial_index`,
which asserts the completed-count query reaches `ix_sessions_mentor_completed`;
after the rewrite it took `ix_sessions_mentor_window` instead.

So every remaining step ends with `ANALYZE` on the tables it rewrote, in both
directions. `ANALYZE` is permitted inside a transaction block — `VACUUM` is not,
and is not what is needed, since the rewrite already reclaimed the space.

**Steps 2 to 4 do not have it**, and that is a known gap rather than an oversight
left unstated. That planner assertion exists only for `sessions`, so nothing was
watching when `users`, `institutions` and `mentor_profiles` were rewritten.
Autovacuum re-analyses them and the tables are small — `users` is 43 rows — so
the exposure is a bounded window of worse plans rather than a lasting one. If a
deploy wants belt-and-braces, a follow-up migration that does nothing but
`ANALYZE` those tables is a safe one-liner; it is not worth destabilising two
open PRs for.

The window matters most where the table is large and the read is hot, which is
`sessions` (step 8), `session_participants` (step 6) and `session_events`
(step 7) — the three still to come.

### `alembic check` sees a missing index, not a wrong one

Measured in step 3, because the distinction decides how much verification the
predicate steps need. Removing the index recreation from the migration failed two
tests, so **a partial index dropped and never recreated is caught**. Recreating it
with the *wrong* predicate is not: `compare_metadata` does not compare `WHERE`
clauses, which `project-conventions` already states as one of its blind spots.

That failure is quiet in the worst way. `ix_sessions_mentor_upcoming` rebuilt as
`WHERE status = 'completed'` still exists, still has its name, still gets planned
against — and simply never matches an upcoming session. The query it serves falls
back to a sequential scan and the rows it should rank go missing from a listing.

`test_every_partial_index_predicate_survives_a_conversion` closes it, comparing
the **literal values** in each predicate against what the model declares rather
than the rendered SQL — because the cast is expected to change
(`'pending_review'::lookup_status` becomes `::text`) and the values are what must
not. Steps 6 and 8 inherit it; step 8 drops four objects and needs it most.

### The ORM stops coercing, and that is invisible

`pg_enum` does more than create the type: the dialect-level `ENUM` turns each row
back into the Python class on read. **A plain `text` column returns `str`**, so
`Mapped[ApprovalStatus]` over `Text` type-checks clean and yields a string. `==`
still works — a `StrEnum` member equals its value — so the defect hides. `is`
does not, and `mentor_status_store.may_self_resume` used identity:

```python
if row is None or row[0] is not ApprovalStatus.APPROVED:
```

Converted naively that is `False` for every mentor, and a self-paused mentor can
never resume: fails closed, so not an exposure, but a feature that stops working
with a green suite. **`str_enum` in `infra/db/types.py` is the answer** and every
step uses it. Audited across the codebase: that was the only identity comparison
reading a database column; the other twenty-nine sit on transform dataclasses
holding real Python enums and are unaffected.

### The migration template

`d1f8a3c62b47` is the reference implementation — copied per step, never imported,
because a shared helper is a live symbol that changes what an old revision does
on replay (decision #43). Per column: drop default → `ALTER COLUMN … TYPE text
USING col::text` → re-add default as a text literal → add `CHECK`; then
`DROP TYPE` once every column is off it. Steps 3, 6 and 8 drop their `pg_depend`
objects first and recreate them after, **with the same name**.

The duplication is answered by verification rather than by abstraction:
`test_every_converted_enum_has_a_check_naming_its_values` and
`test_the_database_agrees_with_how_each_vocabulary_is_declared` assert the
outcome for every converted column, whoever wrote the migration — which is
strictly more than a helper could guarantee.

## Shipped after step 8: `SessionStatus.WITHDRAWN`

**Landed as `e7b4d29c3f16`, three reversible `CHECK` swaps.** The ordering
decision — step 8 first, then the value — is kept below because it is the
worked example of what the conversion bought, and the reasoning applies to every
vocabulary change from here.

A mentee withdrawing a request before it expires has no status today. It lands as
`CANCELLED`, which conflates it with calling off an already-confirmed session — a
different fact carrying different policy for refunds and mentor-reliability
stats. It is reconstructable from `session_events`
(`from_status = 'pending_mentor_approval'`, `actor_type = 'user'`,
`reason_code = 'mentee_no_longer_needed'`) but not queryable as a status.

The ordering matters because the two costs are not comparable:

| When | Cost | Reversible |
|---|---|---|
| Before step 8 | `ALTER TYPE ... ADD VALUE` — one line, works in a transaction | **No.** `DROP VALUE` is unimplemented |
| After step 8 | three `CHECK` swaps in one migration | Yes |

Three, not one: `session_status` guards `sessions.status`,
`session_events.from_status` and `session_events.to_status`, and a `CHECK` cannot
span tables. Still one migration.

**#100's sub-rule did not survive its own success, and that is worth stating.**
*"Do not add a value until something writes it"* was justified by permanence:
`ADD VALUE` could not be undone. Step 8 removed the permanence, so the rule no
longer follows from its own reason. `WITHDRAWN` landed ahead of the withdraw
flow deliberately, and it is reversible until the first row uses it — at which
point the downgrade fails loudly rather than rewriting live rows, which is the
correct behaviour and was verified.

The rule still holds for a vocabulary that is *not* converted. There are none
left in this schema, so in practice it is retired.

**`LIVE_STATUSES` is untouched, and that is the load-bearing part.** A withdrawn
request holds no booking slot, so it sits outside the predicate with `EXPIRED`,
`DECLINED` and `CANCELLED`. Verified against real rows: a mentor **can** be
booked over a withdrawn request, because `sessions_no_mentor_double_booking`
ignores the row. Adding `withdrawn` to that predicate would keep a withdrawn
request blocking the slot it just released.

**The transition rule is `PENDING_MENTOR_APPROVAL -> WITHDRAWN` only.** A
confirmed session called off is `CANCELLED`, whoever calls it off — the mentor
has already committed time, and that is the fact the two statuses separate.
Nothing enforces transitions in the schema; the rule lives at the endpoint that
writes the status, and is recorded on the enum member because the value is
meaningless without it.

`SessionStatus` now has **8 members**. The product's A–H list is 8 states, but
they do not line up one-to-one: "Upcoming" is derived from `CONFIRMED` plus a
future `starts_at` rather than being a status of its own.

Two related cautions recorded at the same time, both about `NO_SHOW`:

- **Session-level and per-person are deliberately two levels.** `NO_SHOW` on
  `sessions.status` says the session did not happen; `session_participants.attendance_status`
  says one named person did not arrive. A mentee-attended, mentor-absent session
  has two participant rows and exactly one session outcome.
- **No synonym for it.** "Uncompleted" as a second name for `NO_SHOW` is how one
  rule becomes two representations, which non-negotiable #8 forbids.
- Nothing currently transitions a past-dated `CONFIRMED` session to `NO_SHOW`.
  That job does not exist yet, and this conversion does not create it.

## Definition of done, per step

- `alembic upgrade head` → `downgrade` → `upgrade` clean
- `alembic check` reports no drift after the models change
- Every dropped index and constraint recreated **with the same name**, verified by
  comparing `pg_indexes` before and after
- A `CHECK` on every converted column naming exactly the values the `StrEnum` has
- A test that the database refuses an unknown value — the whole point of not
  moving enforcement to Pydantic alone
- **The class moved from `PG_ENUM_TYPES` to `TEXT_CHECK_ENUMS` in the same
  commit**, with the new constraint name as its value. The partition test fails
  if it is in both or neither, which is exactly the half-applied state to catch
- **Every plpgsql function naming a dropped type rewritten in the same
  migration.** Nothing checks this for you: function bodies carry no dependency
  records, so the `DROP TYPE` succeeds and the failure waits for the next write
- Full gate green, with `REQUIRE_DB_TESTS=1` set — the `db`-marked tests skip
  silently without `TEST_DATABASE_URL`, and a skipped test reads exactly like a
  passing one

## Already checked, so you do not have to

- **`session_window()` is unaffected.** Its signature is
  `(starts timestamptz, mins integer) RETURNS tstzrange` and its body is a single
  `tstzrange(...)` — it never reads `status`. Only the *predicate* of the
  exclusion constraint that uses it does.
- ~~**No trigger reads an enum column.**~~ **Wrong — see "The trigger this
  document said did not exist" below.** The claim was true of
  `trg_set_updated_at`, which is what was looked at; `trg_apply_mentor_status`
  on `mentor_status_events` was not.
- **Nothing orders by an enum column** — see item 4 above. Re-verified
  2026-08-16 across all 26 `order_by` sites in `infra/db/`: every one sorts by
  `created_at`, a name, a `sort_order`, an id or a similarity rank.
- **`refuse_retiring_a_primary_offering` is unaffected.** The schema's other
  business-rule trigger reads `deleted_at`, `is_active` and
  `primary_session_type_id`, and no enum column. *(It was true throughout the
  conversion and is now moot: that trigger and its column were dropped by
  `c8f1a3e2b904`. Left as written, because this document is the record of what
  the conversion had to consider.)*

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
- **Re-run the trigger check, and read the function bodies rather than the
  trigger names.** `pg_get_functiondef` over every function in `public` is one
  query. The name `trg_set_updated_at` is what made the original audit stop early.

**All four were re-run on 2026-08-16 at `a3c81f7e5b24`**, and the results are
folded into the sections above rather than left here as a promise: the census
held at 17/21/15, every enum is a `StrEnum`, nothing orders by an enum column,
and the trigger check is what found §6. Re-run them anyway at each step — the
point of the list is that it is cheap, not that it has been done once.

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
