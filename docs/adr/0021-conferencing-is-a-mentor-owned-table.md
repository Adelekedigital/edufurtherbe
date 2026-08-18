# 21. A mentor's conferencing options are their own table

Date: 2026-08-17

## Status

Accepted.

Diverges from `docs/edufurther-migration/`, which ADR 0007 makes canonical for
the target data model. `04_sessions.sql` specifies
`session_type_booking_configs.meeting_venue` as *"NULLABLE = inherit from
mentor_profiles.default_meeting_venue"*, and `02_profiles.sql` specifies that
mentor column alongside `custom_meeting_url`. Neither survives. The package is
not edited here (ADR 0007); this record is the departure.

## Context

`meeting_venue` was a single `text` column naming one of four values, and it has
moved three times in three releases — mentor column, per-offering nullable,
per-offering `NOT NULL`. Each move relocated a **label**. None fixed what was
wrong with it.

**Half the vocabulary named capabilities the platform does not have.**

| value | what it could actually produce |
|---|---|
| `google_meet` | a per-session link, minted through the calendar integration |
| `daily` | a per-session link, minted through Daily's API |
| `zoom` | **nothing.** No integration exists and nothing can create one |
| `custom` | **nothing.** It needs a URL, and `mentor_profiles.custom_meeting_url` was deleted by D88's contract step — as a removal rather than a move, because nothing had ever written it |

So two of four values could not produce a joinable session. A mentor could select
a venue that cannot host, and the schema had no way to say so, because one column
cannot distinguish *which provider* from *whether this mentor can host on it*.

That is not a vocabulary problem to be fixed by removing values. `custom` is a
real thing mentors use; what it lacked was somewhere to keep the URL that makes
it reachable. And `zoom` is a real thing mentors will want, once there is a
connection behind it — which needs somewhere to keep the connection.

## Decision

**`mentor_conferencing_options`** — one row per provider per mentor, holding what
a mentor can host on and, where the provider needs it, what is needed to reach
it.

`session_types.conferencing_option_id` references one, nullable, **null meaning
use my default** — the same inherit-from-the-mentor shape
`requires_booking_confirmation` uses, and safe for the same reason: a mentor row
always exists, so the chain cannot bottom out at nothing.
`session_type_booking_configs.meeting_venue` is dropped.

Four consequences worth recording, because each was a choice.

**1. The foreign key is composite, and that decides where the reference lives.**

```sql
FOREIGN KEY (mentor_user_id, conferencing_option_id)
  REFERENCES mentor_conferencing_options (user_id, id)
```

A single-column key is satisfied by *any* option row, including another mentor's.
The composite version makes a cross-mentor reference **unrepresentable** rather
than merely refused by whatever application code remembers to check. That is why
the reference sits on `session_types` rather than on the booking config:
`session_types` already carries `mentor_user_id`, so it costs nothing there, and
the config would need a denormalised column or a trigger to carry the same
guarantee. Venue stops sitting beside duration and notice; database-enforced
integrity is worth that.

`UNIQUE (user_id, id)` on the options table exists for no other purpose than to
be the target of that key. It is redundant as a uniqueness claim — `id` is
already the primary key — and PostgreSQL requires a unique constraint on exactly
the referenced pair.

**2. Resolution has three steps, not two.**

```
offering.conferencing_option_id  →  that option
      ↓ null
mentor's option WHERE is_default
      ↓ missing
'google_meet'                        ← platform fallback; never null
```

Seeding every mentor a default makes the third step look unreachable. *"It cannot
happen because creation always sets it"* is exactly the reasoning that failed for
`primary_session_type_id`: true until the retirement trigger made
release-then-retire a legal state, at which point the venue cascade had a
reachable, empty bottom and a required response field resolved to null.
`SessionTypeRead.meeting_venue` is required, so a resolution that can return null
is a 500 waiting for the first mentor who slips through. Seed **and** fall back.

**3. The `custom_url` check is symmetric.**

```sql
CHECK ((provider = 'custom') = (custom_url IS NOT NULL))
```

`ck_mentor_profiles_custom_url_requires_custom_venue` ran one direction —
`custom_url IS NULL OR venue = 'custom'` — which permitted `custom` with no URL
and left a mentor bookable with nowhere to meet. The other direction matters too:
a URL left on a `google_meet` row is dead data that survives an edit.

**4. A new vocabulary, `ConferencingProvider`, omitting `zoom`.**

`MeetingProvider` answers *where did this session happen* and keeps every value
the platform has written, including ones nothing can create.
`ConferencingProvider` answers *what may a mentor select now*, and a value joins
it only once something can produce a joinable session from it — which is what
`external_account_id`, `status` and `connected_at` are declared to hold. The two
overlap and a test asserts one is a subset of the other, so they cannot drift.

## Consequences

**Migrated `custom` offerings cannot be carried faithfully, and are
quarantined.** There is no URL to put on them and never was, so a `custom` option
row is unsatisfiable. Those mentors are seeded a `google_meet` default — which
keeps them bookable — and named in the migration output and in the ETL report for
a human to follow up. Reported rather than guessed, on the `CalendarSettings`
precedent (settled decision #81). The alternatives were inventing a URL, relaxing
the constraint the table exists to enforce, or silently rewriting the venue with
every count still reconciling.

**The public contract narrows.** `SessionTypeRead.meeting_venue` is typed
`ConferencingProvider`, so `zoom` leaves the advertised response enum. It was
advertised and never producible; removing it makes the contract describe what the
data can hold.

**The three connection columns ship before anything writes them**, which is the
one place settled decision #21 is knowingly not applied. The alternative is a
second migration on a table that already has a composite key pointed at it, and
the columns are what make the `zoom` deferral coherent rather than arbitrary.

**Venue is no longer beside duration and notice.** A reader of
`session_type_booking_configs` no longer sees where the session happens, and has
to follow the offering's reference. That is the cost of the composite key, paid
deliberately.

## Alternatives considered

**Keep the label and add `custom_url` to the booking config.** Cheapest, and
leaves `zoom` still selectable and still unable to host. It also puts a mentor's
credential on a per-offering row, so a mentor with five offerings on the same
custom room keeps five copies of the URL — the duplication non-negotiable #8
calls a defect.

**A single-column foreign key to the options table.** Simpler to declare, and
satisfied by any mentor's row. The guarantee would then live in whatever query
remembered to scope it, which is precisely what "authorization is the query"
exists to avoid — and it would be invisible until one mentor's offering resolved
another mentor's venue.

**`is_default` on the offering side instead of the option side.** Rejected for
the reason settled decision #89 gives for schedules: a flag on N rows lets N-1 of
them disagree, where a flag on the parent is true by construction. Here the
partial unique index makes "exactly one default" a schema fact.

**Drop `zoom` from `MeetingProvider` entirely.** It is in
`sessions.meeting_provider`, which records what a session used. Removing it would
rewrite history to match present capability.
