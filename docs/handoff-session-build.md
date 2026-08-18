# Handoff — building the session flows from `FE-ui-guide/`

**Status:** 11 PRs shipped, 14 remaining. The read surface is complete, the enum conversion is done, and the write surface is still empty — no `POST`, `PATCH` or `DELETE` exists on `sessions` or `session_types`, so a mentor cannot create an offering and a mentee cannot book one.
**Written:** 2026-08-16, from the 14 screens in `FE-ui-guide/` measured against the codebase and
`docs/edufurther-migration/`.
**Companion:** `handoff-enum-to-text-check.md` — its five steps are PRs 4–8 below.

Read this before opening the UI files. It is the record of what was decided and why, so the
decisions do not get re-derived, re-argued, or silently reversed.

---

## The one-line state

**The read surface is largely there; the write surface is empty.** Every screen has a `GET` behind
it. There is no `POST`, `PATCH` or `DELETE` on `sessions` or `session_types` anywhere in the API, so
a mentor cannot create a session type and a mentee cannot book one. Both flows read correctly and
neither can be completed.

Measured: **18 of 46 UI requirements built**, across 29 built tables of the **66 the canonical
package specifies**. The gap is build order, not design.

---

## Read this first, or repeat two mistakes

Both of these were made during the analysis and caught only by going back to the source.

1. **`docs/edufurther-migration/` is canonical (ADR 0007) and specifies 66 tables.** Credits,
   reviews, the intake stack, messaging, booking policies and a full `idempotency_keys` table all
   have finished schemas waiting. Do not describe any of them as undesigned. Only three things in
   the UI are genuinely unspecified: per-session-type scheduling windows, the draft→publish
   lifecycle, and session templates.

2. **A grep count is not a verdict.** Mentor stats were recorded as missing on the strength of a
   keyword probe. They are fully built — `infra/db/session_stats.py` computes them and
   `MentorPublicRead` exposes all four. Confirm against the schema and the route before recording a
   gap.

---

## Six things in the code that read the opposite of their names

These cost real time to find. None of them are guessable.

| Thing | What it actually is |
|---|---|
| `20260812_1000_m4_session_type_idempotency_key` | **Not idempotency.** A partial unique index — `UNIQUE (mentor_user_id, name) WHERE deleted_at IS NULL`. Real idempotency is the canonical `idempotency_keys` table, unbuilt |
| `GET /api/v1/users/{user_id}/session-types` | **Not the mentor's list.** `tags=["public"]`, no token, and filtered through `session_type_is_live()` + `mentor_is_public()` — active offerings of approved-and-listed mentors only. A mentor cannot see their own paused type through it |
| `session_type_is_live(user_id)` | Three predicates: ownership, `is_active IS TRUE`, `deleted_at IS NULL`. **Four callers**, including `slot_store` and `profile_writer` |
| `_live_session_types` ordering | Orders by **`name`**, not `created_at` — deliberately, because name is unique per mentor among live rows, so the ordering is *total* rather than merely usually-stable |
| `Page[T]` | **Cursor**-based (`encode_offset_cursor`, `MAX_SEARCH_OFFSET`), not limit/offset. And the public session-types endpoint does not paginate at all — it returns every row in a `Page` envelope |
| `SessionStatus.EXPIRED` | Exists with **nothing able to produce it**. The response deadline is its missing producer |

**The dangerous one is `session_type_is_live`.** The mentor's own list needs its ownership and
soft-delete predicates but *not* `is_active`. Adding an `include_inactive` flag touches the
predicate that decides what is **bookable** — settled decision #90 requires a switched-off session
type to be "invisible **and** unbookable", and a mis-defaulted flag reaching `slot_store` makes
deactivated types bookable. **Extract a narrower helper and recompose `session_type_is_live` from
it.** Do not parameterise it.

---

## Decisions taken

### Vocabulary — settled

`session_types.category` and `application_stage` are free-text columns with no vocabulary and no
data, deliberately withheld from the public contract because publishing them "would commit this
contract to a shape nobody has designed". The UI has now designed it.

- **`application_stage`** — five values: `early_exploration`, `drafting_stage`, `post_submission`,
  `revisions`, `other`. `other`'s label lives in a new nullable `custom_stage_label`, the two tied
  by `CHECK ((application_stage = 'other') = (custom_stage_label IS NOT NULL))`. Both directions:
  `other` with no label renders a blank chip, and a named stage carrying a stale label is dead data
  that survives an edit.
- **`category`** — a reference to the existing six-row `service_offerings` taxonomy. No new
  vocabulary. "Essay Review" in the mock is `document-preparation`; "Any stage" is likewise mock
  text and **not** a sixth value.
- **`sessions.topic`** — the same taxonomy from the mentee side. "Program and School selection" is
  `program-selection` + `school-selection`.

**Enforcement is staged.** `StrEnum` at the Pydantic boundary now; the database `CHECK` lands
**before `etl/sessions.py` maps either column** — a trigger, not a date. Today the API is the only
writer of these two, so the boundary suffices; the ETL already inserts into `session_types`, so it
is one field-mapping line from not being.

**This diverges from canonical**, which specifies both as plain `text` and has no
`custom_stage_label` at all. The divergence is deliberate and needs an ADR landing in the PR that
implements it — not a separate one (two PRs per decision is overhead).

### Ordering — settled

**Enum step 5 (`session_status`) lands before any endpoint that writes `sessions.status`.** It
recreates `ix_sessions_mentee_upcoming`, `ix_sessions_mentor_upcoming` and the
`sessions_no_mentor_double_booking` exclusion constraint, and every booking transition maps its
`409` off that constraint. Convert once, then build on the settled shape.

### API shape — settled

- **New authenticated router** `api/routes/me_session_types.py`, `prefix="/api/v1/me"`. The existing
  session-types router is public-tagged and cannot host mentor writes. Note `users.py` is already
  `prefix="/api/v1"` and owns `/me`; no collision, but two routers serving `/api/v1/me*` was a
  deliberate call.
- **A non-mentor caller gets `200` with an empty page**, not `403` — matching the reasoning already
  written into the public endpoint: an empty list "is a different and true statement".
- **Authorization is the query, never a check after it** (§5). A row belonging to someone else
  returns `404`, not `403` — `403` confirms the id exists.
- **`is_active` is a `PATCH` field, not a dedicated endpoint.** It is a bare boolean with no cascade:
  deactivating hides the type from new bookings and leaves existing ones alone. Industry practice
  reserves action endpoints for transitions with side effects. **Draft→publish, when it lands, *is*
  such a transition and should get its own endpoint** — do not model it as `PATCH {status}`.
- **`DELETE` refuses with `409`, not `403`.** A mentor is entitled to delete their own session type;
  the refusal is about state and the user can resolve it. `trg_refuse_retiring_a_primary_offering`
  already enforces this at the database level — and it fires on `UPDATE` too, so the `is_active`
  toggle needs the same `409` mapping. The endpoint **translates** that refusal; it does not
  re-implement the rule.

### Booking lifecycle — settled, and recorded nowhere else

| Tab | Status |
|---|---|
| Pending | `pending_mentor_approval` |
| Upcoming | `confirmed` |
| History | every terminal status |

That pairing already exists as `LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"`
in `infra/db/models/sessions.py` — the predicate behind the double-booking constraint. Reuse it.

- **Accept** (mentor only): Pending → Upcoming. **Decline** (mentor only) and **Cancel** → History.
- **Either party may cancel**, from Pending or Upcoming. A mentee can never accept their own request.
- **Nobody may cancel within 10 minutes of `starts_at`.** Time-relative, so no `CHECK` can hold it; a
  trigger can, on the `trg_refuse_retiring_a_primary_offering` precedent.
- **History filters by status only.** Filtering by date or mentor name is wanted eventually and was
  explicitly called not a priority. Do not build it speculatively.
- **Every transition writes a `session_events` row.** The reason vocabulary already exists —
  `mentor_unavailable`, `mentee_no_longer_needed`, `scheduling_conflict`, `technical_issue`,
  `mentor_no_show` — and those codes drive refund policy, so the value identifies *who* withdrew.

### One dashboard, two audiences — settled

The mentee's bookings view is the same shape with the counterparty flipped. `GET
/users/{user_id}/sessions` was already built for it: *"every session this user is a party to — as
mentor, as mentee, or both"*, carrying both ids. **The read shape should expose "the other party",
resolved server-side, rather than a field named `mentee`** — one contract, not two endpoints. Every
gap on that dashboard is fixed once, for both.

**The one asymmetry:** the attendance-rate line on Pending cards is mentor-only — the mentor sees
the *mentee's* rate. `session_stats.py` is mentor-only by design ("the rate is the mentor's own"),
so the mentee-side aggregate is the missing mirror. There is no matching gap in the other direction.

---

## Session lifecycle — decided 2026-08-16

### Two windows, and an earlier draft conflated them

They govern different things, apply to different sessions, and neither is the other's fallback.

| | applies to | decides |
|---|---|---|
| **Response window** | confirmation-required offerings only | when an unanswered *request* dies |
| **Join window** | every session, once confirmed | whether a party was **present** |

An earlier draft of this document had a single `grace` subtracted from `starts_at` doing both jobs.
There is no such value. It is deleted rather than corrected, because a reader finding it would
reconstruct the wrong model.

### A. The response window

Runs from the request being made until **`starts_at − W`** — measured **backwards from the session**,
not forwards from creation. The guarantee is to the *mentee*: you will know before the session,
early enough for the answer to still be useful. A window measured from `created_at` guarantees the
mentee nothing.

- **W is a platform value today and a mentor preference later.** Legacy default 24h; moving to 6h.
  The final number is **still open**.
- **Reminders fire during the window** so the mentor can act. The schedule is **still open**.
- **On elapse the session becomes `expired`**, surfaced to users as **"Unconfirmed"**, and both
  parties are told. `expired` already exists in `SessionStatus` with nothing able to produce it;
  this is its producer.
- **Auto-confirming offerings have no response window at all.** Nothing is awaiting an answer.

**The window cannot be born empty, and that is why the booking notice below matters.** If a request
could be made closer to the session than `W`, `starts_at − W` would already be in the past and the
request would expire before the mentor saw it. A 24-hour booking notice against a `W` of 24h or less
makes that unreachable. **When `W` becomes a mentor preference, it is bounded by that mentor's
booking notice** — the two cannot be set independently.

### B. The join window

**`starts_at − 5 minutes` to `starts_at + 15 minutes`**, for every confirmed session. Later a mentor
preference.

It decides attendance, and therefore the difference between **Completed** (both present) and
**Missed** (either absent). `session_participants` already carries `joined_at`, `left_at` and
`attendance_status`; **nothing computes them for a live session** — the ETL only maps legacy values.
That rule lands with the transitions.

### Booking notice is a platform rule: 24 hours

No same-day booking. **Implemented as the rolling duration already built** — `min_notice_minutes`,
enforced by `slot_store` against `now` — and *not* as a calendar-date comparison. Three reasons, in
the order they mattered:

1. **A calendar-date rule needs a timezone** and mentor and mentee are in different ones, so it
   would introduce a second timezone rule into a system that already settled one.
2. **It protects inconsistently.** A 09:00 Tuesday session booked 23:00 Monday is ten hours' notice
   on a different date and would be allowed; a 23:00 Tuesday session booked 00:01 Tuesday is
   twenty-three hours' notice on the same date and would be refused.
3. **It does not generalise.** The UI's lead-time options are 6/24/48/72 **hours**. "Not the same
   day" has no six-hour form, so it would be replaced the moment lead time became editable.

**The migration does not carry this rule.** `min_notice_minutes` defaults to **120**, the ETL never
sets it, and all five offerings in the dev load are 120 — so every migrated mentor would permit
booking two hours out, against a platform rule of twenty-four. Nobody chose that; it is a column
default nobody revisited, and it is invisible because 120 is a valid value and every count
reconciles. **The default becomes 1440 and migrated rows are backfilled.**

### The eight statuses

Seven of eight exist. Only `withdrawn` is missing.

| UI | stored | |
|---|---|---|
| Upcoming | `confirmed` | |
| Pending | `pending_mentor_approval` | |
| Cancelled | `cancelled` | either party, after confirmation |
| Missed | `no_show` | date passed, either or both absent — also "uncompleted" |
| Completed | `completed` | both attended, no absentees |
| **Unconfirmed** | `expired` | **label differs from the stored value, deliberately** |
| Declined | `declined` | mentor, before the request expires |
| **Withdrawn** | — | **missing.** Mentee withdraws before the request expires |

**`withdrawn` lands with or after enum step 5, never before.** `session_status` is still a
PostgreSQL enum and there is no `ALTER TYPE ... DROP VALUE`, so adding it now is permanent; adding
it after the `text` + `CHECK` conversion is an edit.

**Whether it should be a status at all is open.** It differs from `cancelled` by *when*, not by
*who* — a pending request rather than a confirmed session. `SessionReasonCode` already exists and
those codes drive refund policy, so `cancelled` + a reason is the alternative. A status is visible
to tab filters and aggregates without a join; a reason code avoids growing a vocabulary that cannot
shrink. Decide with the transitions.

## Per-session-type booking config — decided 2026-08-16

From `sessionTypeCreationUI/SESSIONS screen 4`. **Build it now**: per-offering settings are part of
the new experience and move the platform toward monetising for mentors.

### Windows replace general availability, they do not intersect it

An offering's scheduling windows **take precedence over** `availability_rules`. The whole point is
that each type carries its own settings.

| offering has windows | bookable when |
|---|---|
| yes | **its windows only** — general availability does not apply |
| no | general availability, as today (the screen's "use general availability" toggle) |

**"Restrict" in the screen's copy is misleading and the mock's own example shows why.** Wednesday
5–8PM and Thursday 9AM–1PM read as deliberate evening and morning slots; intersected with a normal
working-hours availability they would yield **zero slots**, and the mentor would see an empty
calendar with nothing explaining it.

**We must show or inform the mentor which mode an offering is in.** A mentor who sets windows on one
offering and expects their general availability to still apply has been surprised by us, not by
themselves — and precedence that is only true in the backend is precedence nobody can act on.

It reads as a UI obligation and it has an **API** consequence: the offering read model has to say
*which* mode is in effect, so a client can state it. The mentor's own session-type list should be
able to show "uses its own windows" against "uses your general availability" without a second
request or a client-side inference from an empty array.

**Two things follow that this decision does not by itself settle:**

- **A mentor with windows and no general availability is bookable.** That is now reachable and must
  not be treated as a misconfiguration.
- **`availability_exceptions` still subtract, always.** Windows replace *availability*, not
  *unavailability* — a mentor blocking a date has blocked it for every offering. Derived rather than
  stated, and written here so "replace" is not read as replacing everything.

### Minimum booking lead time: platform floor 24h, mentor sets 24h–72h

The platform rule stays **24 hours**. Mentors may raise it **per session type**, to a maximum of
**72 hours**. The ceiling may be lifted later.

- **The mock's 6-hour option is dropped.** It sat below the platform rule; the option list becomes
  24 / 48 / 72.
- `min_notice_minutes` therefore needs **a bound it has never had** — it has no `CHECK` at all today
  and can be set to zero. Range: **1440–4320 minutes**.
- The default moves 120 → **1440**, with migrated rows backfilled (shipped, #100).
- **The floor is what keeps the response window coherent.** A confirmation-required offering promises
  the mentee an answer by `starts_at − W`; a mentor who could accept 6-hour bookings while promising
  24-hour notice would generate requests that expire before they are seen. With the floor at 24h and
  `W` at or below it, that state is unreachable rather than merely unlikely.
- The screen shows discrete chips; **the column stores a bounded value.** Chips are presentation, as
  with duration.

### Duration stays free

Already settled: the mock's 30/45/60/90 are presentation, mentors set their own, and
`duration_minutes` keeps `CHECK BETWEEN 5 AND 480`.

### What building windows costs

**A new table with no canonical schema.** Per-session-type scheduling windows are one of only three
things in the UI the package does not specify, so this diverges from canonical and **needs an ADR
landing in the PR that implements it** (never a separate acceptance PR).

It also changes **slot computation**, which is built and tested — `slot_store` currently reads
`availability_rules` unconditionally. That is the risk in this work, not the table.

**Sequenced as its own PR, immediately after offering CRUD.** CRUD needs no migration; windows need
a migration, an ADR, and a change to the slot query. Landing them together would put a schema change
and a rewrite of a tested read path into a PR that otherwise has neither.

## D88 is reversed for two of its three fields — decided 2026-08-17

**One fallback mechanism covering three fields was the mistake, not any individual
placement.** D88 said *an offering without its own X uses the primary offering's X*, and each
field then went somewhere else on contact:

| field | ended up |
|---|---|
| `meeting_venue` | **per offering**, `NOT NULL`. Its cascade had a reachable, empty bottom (#102) |
| `custom_meeting_url` | **nowhere** — deleted rather than moved, because nothing read it |
| `requires_booking_confirmation` | **per mentor**, reversing here |

Recorded so the one-mechanism idea is not re-proposed: it is the premise that failed, three
times, in three different ways.

### Confirmation returns to `mentor_profiles`, with an optional per-offering override

`mentor_profiles.requires_booking_confirmation` comes back as the authority, `NOT NULL`.
`session_type_booking_configs.requires_booking_confirmation` becomes **nullable, and null means
inherit**. Every session type follows the mentor unless it overrides.

**The D88 argument against a nullable boolean does not apply here, and that is the whole
point.** It said *"a boolean has no room for a third state, so null would mean inherit and be
indistinguishable from false"* — true when inheriting from a **primary offering**, because a
mentor can legitimately have live offerings and no primary, so the chain bottoms out at nothing.
A mentor row always exists. **The terminus cannot be missing**, which is exactly what killed the
venue cascade and exactly what this one has.

The fan-out in `profile_writer` is **deleted**. It existed because the column had moved while the
mentor-level toggle was still the only control; with the mentor column authoritative, the toggle
writes one row and nothing needs copying. Two writers for one column is the duplication
non-negotiable #8 calls a defect.

The UI may not expose the per-offering override yet. The column carries it regardless — a null
costs nothing and the alternative is a second migration when the UI catches up.

### `primary_session_type_id` is dropped, with its trigger

**Measured before deciding: three consumers in production code, and after the change above,
zero.**

| | was |
|---|---|
| `profile_store.py` | read booking settings off the primary — moves to the mentor column |
| `etl/sessions.py` | set the pointer at load |
| `models/mentoring.py` | the column and `trg_refuse_retiring_a_primary_offering` |

**Nothing lands a mentee on it and nothing orders by it.** `_live_session_types` orders by `name`,
which is unique per mentor among live rows and therefore total. A session type is *active* or
*inactive*; mentees see what the mentor created, in name order.

Consequences, both deliberate:

- **`default_meeting_venue` leaves the owner profile response.** It was read from the primary, and
  venue is per-offering with no mentor-level home. The owner sees venue per offering in
  `/me/session-types`. This removes a field from a shipped response.
- **`trg_refuse_retiring_a_primary_offering` goes**, and with it the two-step
  release-then-retire dance. Deactivating an offering becomes a plain `is_active` toggle, which is
  what `DELETE`'s and `PATCH`'s `409` mapping no longer has to translate.

### Settled decisions this amends

**#88**, **#92**, **#102**, **#58** all describe the primary offering or the cascade. Each is
amended in the PR that implements the change, not in a separate one.

## Conferencing becomes a mentor's own table — decided 2026-08-17

**`meeting_venue` was a label, and half its vocabulary named capabilities we do not have.**
`google_meet` and `daily` are minted by the platform and need nothing from a mentor; `zoom` has no
integration and nothing can create one; `custom` needs a URL and `custom_meeting_url` was deleted
by D88's contract step. So two of four values could not produce a joinable session, and the column
could not tell the difference between *which provider* and *whether this mentor can host on it*.

### `mentor_conferencing_options`

| column | type | |
|---|---|---|
| `id` | `uuid` PK | `uuid_generate_v7()` (ADR 0015) |
| `user_id` | `uuid NOT NULL` | → `mentor_profiles(user_id)`, following `session_types` and `availability_rules` |
| `provider` | `text NOT NULL` | `CHECK IN ('google_meet','daily','custom')` |
| `is_default` | `bool NOT NULL` | default `false` |
| `custom_url` | `text NULL` | the static room |
| `external_account_id` | `text NULL` | **future** — the connected Zoom or Teams account |
| `status` | `text NOT NULL` | **future** — `active` / `expired` / `revoked` |
| `connected_at` | `timestamptz NULL` | **future** |

**Preference now, connections later.** The three connection columns are declared because the table
exists to hold them the moment a provider needs authenticating; until then every row is a
preference and `status` is always `active`. `zoom` and `teams` **enter the vocabulary at the point
they have a connection behind them, not before** — a mentor must not be able to select something
that cannot produce a session.

Four constraints, three of them load-bearing:

- `UNIQUE (user_id, provider)` — one row per provider per mentor.
- `UNIQUE (user_id, id)` — **exists only so the composite key below can reference it.**
- partial `UNIQUE (user_id) WHERE is_default` — exactly one default, true by construction rather
  than by application code.
- `CHECK ((provider = 'custom') = (custom_url IS NOT NULL))` — **symmetric**. The old
  `mentor_profiles` CHECK ran one direction only and permitted `custom` with no URL, leaving a
  mentor bookable with nowhere to meet. That gap is recorded at `MeetingProvider.CUSTOM` and this
  is where it closes.

### `session_types` gains the reference, and the config loses the label

`session_types.conferencing_option_id`, `uuid NULL`, **null meaning use my default** — the same
inherit-from-the-mentor shape confirmation now uses. `session_type_booking_configs.meeting_venue`
is dropped.

```sql
FOREIGN KEY (mentor_user_id, conferencing_option_id)
  REFERENCES mentor_conferencing_options (user_id, id)
```

**The key is composite, and that is the reason the reference sits on `session_types` rather than
on the booking config.** A single-column foreign key is satisfied by *any* option row, including
another mentor's; the composite version makes that unrepresentable. `session_types` already
carries `mentor_user_id`, so it costs nothing there and would cost a denormalised column or a
trigger on the config. Venue stops sitting beside duration and notice, and database-enforced
integrity is worth that.

### Resolution, and why it has three steps rather than two

```
offering.conferencing_option_id  →  that option
      ↓ null
mentor's option WHERE is_default
      ↓ missing
'google_meet'                        ← platform fallback; the answer is never null
```

**Seeding every mentor with a default row is not sufficient on its own.** That is the guard that
already failed once: *"it cannot happen because creation always sets it"* was true of
`primary_session_type_id` right up until the retirement trigger made release-then-retire a legal
state, and the venue cascade then had a reachable, empty bottom. `SessionTypeRead.meeting_venue`
is a required field, so a resolution that can return null is a 500 waiting for the first mentor
who slips through. Seed **and** fall back.

### The cost, stated

`meeting_venue` moves for the **fourth** time — mentor column, per-offering nullable, per-offering
`NOT NULL`, now a foreign key. The first three moved a *label*. This one stops storing a label at
all and stores a reference to something the mentor configured, which is what lets `custom` carry a
URL and stops `zoom` being selectable before it works.

**Sequenced before offering CRUD.** A mentor picking a venue should be picking from their own
options, and building the write path against a column that is about to become a foreign key is how
that column moves a fifth time.

## The platform Google account stays a consumer Gmail — decided 2026-08-17

**Measured, not assumed:** the ADR 0012 spike created a calendar, created an event, invited two
external guests and minted a Meet link from `edufurtherlearning@gmail.com` — a consumer account —
and **the invitations arrived**.

Google's published limits are the Workspace ones: 600 API requests per minute per user, creation
throttling past 100,000 events, external invitations throttling at 10,000, roughly 2,000 emails to
external guests per 24 hours. The consumer figures are **not published**, so "tighter limits" is an
unknown rather than an observed ceiling — and at 44 mentors an anti-bulk-abuse throttle is
unlikely to be reached.

**Two things make this safe to defer rather than pay for now:**

- **The decision is cheaply reversible.** Mentors grant `calendar.freebusy` on their *own*
  accounts; the platform identity is ours alone. Moving it to Workspace later changes one stored
  refresh token and **re-consents nobody**.
- **A failed calendar write must not fail the booking.** If Google ever throttles, the session
  still exists and the calendar event retries. That belongs in the booking PR and is what makes
  the limit question stop mattering.

Worth knowing rather than acting on: every limit above is **per user**, and ADR 0012 funnels every
booking through one account, so they are a single bucket rather than spread across mentors.
Concentration is the risk, not volume.

## The PR sequence

Each gets its own checklist and DoD before implementation (§2). ~~As text, with no tool calls in
the same response.~~ — **struck by CLAUDE.md rule 7 (2026-08-17):** the checklist and DoD are still
written first at every tier, but only **Tier 1** waits for approval. Eight of the fourteen below are
Tier 1 and still stop; the six Tier 2 rows post and continue.

**"One at a time, never stacked" is struck.** CLAUDE.md rule 1 reversed it on 2026-08-16 and this
line outlived the reversal. Stacking is allowed: branch from the PR you build on, merge in order.
What survives is the price — **Alembic's chain is linear**, so two open PRs each adding a migration
off the same head produce two heads and `alembic upgrade head` fails outright. Seven of the
thirteen below carry a migration, so a stacked one fixes its `down_revision` at merge time, in
merge order.

### Shipped

| PR | | |
|---|---|---|
| **#87** | `GET /api/v1/me/session-types` | the mentor's own offerings, inactive included |
| **#100** | booking notice floor — default 1440, sanity bound, backfill | |
| **#88, #89/#91, #95, #97, #102, #104, #105, #106** | the enum conversion, all eight steps | no PostgreSQL enum types remain |
| **#107** | `SessionStatus.WITHDRAWN` | the first value added after the conversion |

**Verified from a clean `base` → `head` build**, not from an incrementally upgraded database: zero
columns on an enum type. An incrementally upgraded dev database that has had branch migrations
applied is not a trustworthy oracle — it reported four surviving types that a clean build did not.

### Remaining

| # | PR | Migration | Tier |
|---|---|---|---|
| 12 | confirmation to `mentor_profiles`, config column nullable, fan-out deleted | yes | 1 |
| 13 | drop `primary_session_type_id` and its trigger; venue leaves the owner profile | yes | 1 |
| 14 | `mentor_conferencing_options`; `session_types.conferencing_option_id`; `meeting_venue` dropped | yes | 1 |
| 15 | `POST` / `PATCH /me/session-types` | no | 2 |
| 16 | `DELETE /me/session-types/{id}` | no | 2 |
| 17 | `custom_stage_label`, `category` → `service_offerings` FK, + ADR | yes | 1 |
| 18 | the intake stack — four canonical tables | yes | 1 |
| 19 | intake question endpoints, keyed on a session type | no | 2 |
| 20 | per-session-type scheduling windows + ADR | yes | 1 |
| 21 | `idempotency_keys` + `POST /sessions` | yes | 1 |
| 22 | the four transitions — accept, decline, cancel, withdraw | no | 2 |
| 23 | response deadline: `respond_by`, the expiry producer, reminders | yes | 1 |
| 24 | join-window attendance — `−5 min` to `+15 min` | no | 2 |
| 25 | mentee attendance rate, and "New mentee" for null | no | 2 |

**Order is not preference.** 14 before 15, because a mentor picking a venue should be picking from
their own options, and building the write path against a column about to become a foreign key is
how `meeting_venue` moves a fifth time. 12 before 13, because the owner profile loses its only source
otherwise. 15 after 13, so writes are built against a shape that is not about to lose a column. 17
after 15, so the vocabulary lands on an endpoint that already exists. 21 after 17, because a
booking references a session type whose contract should be settled. 22 after 21 — there is nothing
to transition until something can be created.

**Deferred deliberately, each with a reason recorded above:** credits (`credit_lots`,
`credit_transactions`), reviews, messaging, `booking_policies`. All four have canonical schemas
waiting; none blocks booking.

### The regression each PR can cause, and what catches it

Named per PR, because "run the gate" is not a plan. Each is a thing that would pass every count
and still be wrong.

**12 — confirmation moves.** A backfill is invisible to the suite: every test creates a fresh row
that takes the *default*, and a fresh migrate-then-load runs migrations before any data. The
mentor column must be seeded from what the offerings currently hold, not from `false`, or every
mentor who required confirmation silently stops. *Catches it:* a test that upgrades to the prior
revision, writes offerings at `true`, and upgrades — the shape PR #100 needed for the same reason.

**13 — the primary drops.** `default_meeting_venue` leaves `MentorProfileRead`; a client reading
it gets a missing field rather than a null. And `trg_refuse_retiring_a_primary_offering` going
means deactivation stops being refused — the two-step release-then-retire becomes a plain toggle.
*Catches it:* the guard's four existing tests must be **deleted deliberately**, each recording
what would bring it back, not quietly dropped because they turned red.

**14 — the conferencing table.** `meeting_venue` moves for the fourth time, and the resolution
gains a fallback chain. The migration must **seed every mentor a default row from their current
`meeting_venue`** before dropping the column, or every migrated offering resolves to the platform
fallback and mentors on `daily` or `custom` silently become Google Meet. *Catches it:* a fresh
migrate-then-load compared against the baseline — `google_meet` 1, `daily` 3, `custom` 1 — which is
the check that caught the silent `allmentors` bug in #100.

**15 — the writes.** `session_type_is_live()` has four callers including `slot_store`. The
owner-facing path needs ownership and soft-delete but **not** `is_active`; a mis-defaulted flag
reaching `slot_store` makes deactivated types **bookable**, against #90. *Catches it:* reuse
#87's narrower helper — **do not parameterise**. This PR also owns the `min_notice_minutes`
boundary bound deferred out of #100, and must create the booking config in the same transaction:
`/slots` inner-joins it, so an offering without one is invisible and unbookable.

**16 — the delete.** Refuses with `409` when non-terminal sessions are booked, **carrying a reason
code** — two refusals a client cannot otherwise tell apart. *Catches it:* a test per reason, not
one per status code.

**17 — the vocabularies.** Three docstrings assert these columns are "free text with no
constraint, no vocabulary and no value anywhere" and all three become false. Publishing them is a
**public contract change**: #92 excludes both and a test asserts they never appear in the body.
*Catches it:* update all three copies here, and change the allowlist test deliberately.

**18 — the intake stack.** Four tables landing together. *Catches it:* the canonical DDL declares
`session_type_question_options`, which the UI's two question types do not use — build what is
specified or record why not, but do not silently drop a table from a package that ADR 0007 makes
canonical.

**19 — the question endpoints.** Max five questions per session type is a product rule with no
column to hold it. *Catches it:* enforced at the boundary, and a test that the sixth is refused.

**20 — the windows.** `slot_store` is built and tested, and every existing slot test assumes
`availability_rules` is the only source. Windows **replace** it. *Catches it:* an offering with no
windows must produce **byte-identical** slots to today — that is the regression test, not a
feature test — plus a test that `availability_exceptions` still subtract when windows are in
effect.

**21 — booking.** `sessions_no_mentor_double_booking` is an `EXCLUDE` over `LIVE_STATUSES`; every
booking maps its `409` off it. Conflict checks must run **before** the write, because free/busy is
eventually consistent and a flow can read its own write as absent. *Catches it:* a test that two
bookings for one slot produce one session and one `409`.

**22 — the transitions.** Every one writes a `session_events` row, and the reason codes drive
refund policy. A mentee may never accept their own request; either party may cancel; nobody may
cancel within ten minutes of `starts_at` — time-relative, so a trigger rather than a `CHECK`.
*Catches it:* one test per illegal transition, not one per legal one.

**23 — the deadline.** `respond_by` must be a **stored column with a sweep**, not derived: a
pending request past its deadline whose status was never written keeps holding the mentor's slot
forever, and constraint predicates must be `IMMUTABLE` while `now()` is `STABLE`. *Catches it:* a
test that an expired request stops blocking its slot.

**24 — attendance.** The join window decides Completed against Missed, and `session_participants`
already carries `joined_at` and `attendance_status` with nothing computing them for a live
session. *Catches it:* a test per outcome — both present, one absent, both absent.

**25 — the mentee rate.** `session_stats` is mentor-only by design. The mirror must render null as
**"New mentee"**, never `0%` — zero says "never shows up", null says "no data". *Catches it:* the
null case asserted explicitly, which is how the mentor-side rate was got right.

## Documentation regression to carry into PR 16

Three places assert that `category` and `application_stage` are "free text with no constraint, no
vocabulary and no value anywhere in the data":

- `infra/db/session_type_store.py` — `_live_session_types` docstring
- `infra/db/session_type_store.py` — module docstring, ~line 59
- `api/schemas/session_types.py` — module docstring

**PR 16's migration makes all three clauses false.** One fact, three copies (§8). Update all of
them in that PR or leave a lie sitting beside the code.

---

## `DELETE` with future bookings — settled

**`DELETE` refuses with `409` when non-terminal sessions are still booked on the type**, on top of
the existing primary-offering refusal. A mentor is entitled to delete their own session type; the
refusal is about state, and the mentee holding a booking is why.

Whether that `409` carries a reason code is **open** — see the register below. An earlier revision
recorded it here as settled, on the grounds that the mock explains the failure to the mentor. That
was the wrong basis: **the UI is a guide, not a contract**, and a decision derived from a mock is a
decision nobody took.

*The response window was also recorded in this section, in a model measured forward from
`created_at` with a `grace` before the session. Both are wrong and both are replaced by
**Session lifecycle** above — kept as a pointer rather than a correction, because the arithmetic was
the visible part and the model underneath it was the mistake.*

## Still open

Nothing below blocks PR 12. Each is named where it will be decided.

| | Open | Decide with |
|---|---|---|
| 1 | **`W` — the response window.** Legacy 24h, moving to 6h, mentor's choice later | the transitions |
| 2 | **The reminder schedule** during that window — how many, when | the transitions |
| 3 | ~~Is the 24-hour notice a floor or a default?~~ **Answered: a floor.** Mentors raise it per session type, 24h–72h; the mock's 6-hour option is dropped | *closed* |
| 4 | ~~`withdrawn`: status, or `cancelled` + reason code?~~ **Answered: a status.** Shipped in #107 | *closed* |
| 5 | **Expiry mechanism** — lazy-on-write plus a slow sweep, or a fast sweep alone | the transitions |
| 6 | ~~Does `DELETE`'s 409 carry a reason code?~~ **Answered: yes.** Two refusals — primary offering, and sessions still booked — are otherwise indistinguishable to any client. Note the first of those disappears with PR 13 | *closed* |
| 7 | **`DELETE` with future bookings refuses with `409`** — settled. What is open is only #6, how the refusal explains itself | PR 15 |

### Where the notice rule is enforced — settled

**The product rule lives at the Pydantic boundary; the database holds a sanity bound only.**

| | enforces | value |
|---|---|---|
| `MentorSessionTypeWrite` | the **product rule** | `Field(ge=1440, le=4320)` — 24h to 72h |
| `session_type_booking_configs` | **physical sanity** | `CHECK (min_notice_minutes BETWEEN 0 AND 43200)` |
| column default | the platform floor | `1440` |

Measured against the three tests it had to pass:

- **Fits now.** The API is the only writer this column will ever have — the ETL does not set it and
  no script does. So the boundary *is* the enforcement point in practice. It also kept #100 small:
  the 54 fixture call sites defaulting to `0` stay legal, and the 20 slot tests keep exercising
  short-notice behaviour **directly** rather than through a 24-hour window that hides it.
- **Scales.** `booking_policies` is already in the canonical package. When the 24–72 range moves
  there it is a config change, not a migration. A `CHECK` carrying the product rule would need a
  migration every time the product changed its mind — which is the definition of the wrong home.
- **Standard.** The database refuses what is *impossible*; the application refuses what is
  *disallowed*. Putting policy in a `CHECK` conflates the two, and #100 already puts closed-set
  enforcement at the Pydantic edge for the same reason.

**The sanity bound is 30 days (43200), not the 7 days first proposed.** A `CHECK` exists to catch
corruption — a negative value, an absurd one — and must never be the thing blocking a product
decision. The ceiling is already expected to rise past 72h "maybe later"; a 7-day bound leaves
2.3× headroom and would plausibly need a migration to move, which reintroduces exactly the coupling
this split exists to avoid.

### Closed since the first draft

- **`actor_type` needs nothing.** Four members exist — `USER`, `ADMIN`, `SYSTEM`, `API` — and
  `SYSTEM` is already produced by `_terminal_actor`. Its docstring names this exact case. An earlier
  revision of this document claimed only `USER` existed; that came from reading a truncated `grep -A`
  window, against this document's own rule that *a grep count is not a verdict*.
- **`idempotency_keys` needs a surrogate key.** Canonical specifies `key text PRIMARY KEY`; ADR 0015
  admits no exception and non-negotiable #10 prescribes the resolution — `id uuid` with the
  invariant re-declared as `UNIQUE (key)`. Recorded so it is not re-argued: that rule has been
  overridden twice before with every gate green.
- **Credits, reviews, messaging, the intake stack and booking policies are all specified** in the
  canonical package. The header's "5 credits" is the *mentee's* balance. Do not describe any of them
  as undesigned — this document opens with that warning and it was still missed once.
- **Duration is not a fixed vocabulary.** The mock's 30/45/60/90 chips are presentation; mentors set
  their own, and `duration_minutes` keeps its `CHECK BETWEEN 5 AND 480`.
- **`category` reflects `service_offerings`.** No new taxonomy.

### The response deadline, in short

**Still true, and the reason the deadline must be a stored column with a sweep rather than a derived
value:** `sessions_no_mentor_double_booking` is an `EXCLUDE` over `LIVE_STATUSES`, so a pending
request past its deadline whose status was never written **keeps holding the mentor's slot forever**.
Folding the deadline into the constraint predicate is not possible — PostgreSQL requires those to be
`IMMUTABLE` and `now()` is `STABLE`. A trigger cannot do it either, since nothing writes to an
abandoned row.

```sql
ALTER TABLE sessions ADD COLUMN respond_by timestamptz;
CREATE INDEX ix_sessions_pending_respond_by ON sessions (respond_by)
  WHERE status = 'pending_mentor_approval';
```

**`respond_by = starts_at − W`.** An earlier draft had
`LEAST(created_at + response_window, starts_at − grace)`; both halves of that are wrong. The window
is measured backwards from the session (§A above), and `grace` does not exist.

**The sweep interval was overstated here as "the worst-case extra slot blocking".** Against a
24-hour window five minutes is noise. It only bites where the deadline is close to the request, and
a cheaper mechanism exists: **the booking write can expire stale rows for that mentor inside its own
transaction before inserting** — self-healing at exactly the moment it matters, with a slow sweep
behind it so a mentor's pending list does not show dead requests and the mentee is still told.
**Open**: lazy-on-write plus a slow sweep, or a fast sweep alone.

The expiry event carries **no `reason_code`** — the status already says it, most events legitimately
carry none, and #100 says not to add an enum value until something writes it. Its actor is
`ActorType.SYSTEM` with a null `actor_id`, which already exists and is already produced.

## Working conditions

The repository is shared. During this analysis the branch changed twice and `.gitignore` changed
between two consecutive commands. **Use `git worktree` rather than switching the checkout**, per
CLAUDE.md. Check `git status` and the current alembic head before trusting anything in this document
— the head moved once while it was being written.
