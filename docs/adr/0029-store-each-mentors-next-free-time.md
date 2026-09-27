# 29. Store each mentor's next free time, and poll free/busy to keep it

Date: 2026-09-27

## Status

Accepted.

Supersedes ADR 0004 **only** on "never polled", and only for this one purpose.
Settled decision #15 is narrowed to match. ADR 0004's other decisions stand: a
calendar is still never *mirrored*, and booking still reads free/busy at the
moment of decision.

## Context

The explore screen's mentor card shows "Next available: Today, 12:00 pm". The
owner asked for it to stay rather than be dropped, and chose a stored value
refreshed on a schedule over computing it per card per page load.

Computing it live means projecting slots and making a Google free/busy call for
every mentor on every page, on an endpoint that takes no token. That is slow,
and it lets anonymous traffic drive Google API calls in the platform's name.

ADR 0004 rejected polling on two grounds. **Cost:** Composio billed per call,
and polling 44 mentors every fifteen minutes was estimated at ~127,000 calls a
month against a 20,000 allowance. **Freshness:** a poller is staler than a read
at the moment of decision. It named a proactive feature as the case that would
justify a poll "for that purpose alone", priced then.

Both grounds have moved:

- **Composio is gone**; Google is called directly. Google's own documentation
  states that standard Calendar API use costs nothing, with 1,000,000 requests
  a day per project and 600 a minute per user. At 50 mentors every five minutes
  this job makes 14,400 calls a day, 1.4% of that. At 2,000 mentors it makes
  576,000. Google says it will begin charging above the daily threshold later
  in 2026; that is the number to watch.
- **Freshness is kept where it decides anything.** Booking still reads live
  slots. The stored value decides nothing: it is a display hint, and it is
  withheld whenever it cannot be vouched for.

Nango, an open-source integration layer, is under consideration as a future
way to reach Google. If it is adopted, re-price this job against it.

## Decision

1. **`mentor_next_availability`** stores one row per bookable mentor:
   `next_available_at`, the first instant any live offering could be booked,
   from the mentor's own today over `MAX_PROJECTION_DAYS`; `bookable_until`,
   that slot's start less the offering's notice; and `computed_at`. The time
   comes from `list_slots`, the function booking uses, and the range starts at
   `mentor_today()`, the function `list_slots` uses, so the card searches
   exactly what `/slots` answers.
2. **`mentor_availability_changes` is an append-only log.**
   `trg_log_availability_change` appends to it on the nine tables that decide
   when a mentor is free: `sessions`, `availability_rules`,
   `availability_exceptions`, `session_types`, `session_type_booking_configs`,
   `session_type_scheduling_windows`, `mentor_profiles`, `users` and
   `calendar_connections`. An `UPDATE` that changes nothing but `updated_at`
   logs nothing. `mentor_profiles`, `users` and `calendar_connections` log only
   for the columns that decide availability, pinned by a test against what
   `mentor_is_public()` reads. `sessions` logs nothing for a session that had
   already ended. A trigger rather than application hooks, because a hook
   missed in one of many write paths is silent; a test pins the table list
   against `pg_trigger`.
3. **The card vouches or says nothing.** A refresh snapshots the mentor's
   change rows before it computes, then writes its answer and deletes exactly
   those rows. The card shows the time only while the mentor has no change rows
   left and `bookable_until` is still ahead. `next_available_state` tells the
   client which of `open`, `none` and `refreshing` a null means.
4. **A QStash job, `refresh-next-available`, every five minutes** (the manifest
   cron, overridable per environment like every other job). It recomputes
   mentors never computed, changed since, or older than
   `NEXT_AVAILABLE_MAX_AGE_MINUTES` (default 5, compared with a minute's
   tolerance for delivery jitter, so the default means every run). Age is the
   only thing that catches a change made in Google, which fires no trigger.
   Five minutes because QStash's free tier allows 1,000 messages a day: every
   five minutes is 288, every minute would be 1,440.
5. **One free/busy call per mentor per refresh**, shared across their
   offerings, on a worker thread. **Only intervals are read, and nothing from
   Google is stored**: the row holds one derived instant, so "never mirrored"
   still holds. One builder serves both this job and the API's slot reads.
6. **No lock spans a Google call, and a timeout keeps what it finished.** Each
   mentor is computed, then written and committed on its own; computing only
   reads. A dry run hands the calendar reader no session factory, so even a
   dead-grant write lands in the session the dry run rolls back.

### Two designs rejected in review

- **Comparing the refresh's start time against a `changed_at`.** It blessed a
  taken slot when a booking committed after the refresh had read the data, and
  whenever the app host's clock ran ahead of the database's.
- **Comparing a `changed_at` for equality.** That closed both holes, but every
  booking bumped the mentor's one cache row and held its lock for the whole
  booking transaction, including the meeting-provisioning call. Two mentees
  booking the same mentor at different times queued behind each other.

The log closes all three: a change the snapshot missed is a row nobody deletes,
no clock is compared, and appends do not block one another.

## Consequences

- A card can lag a Google-side change by up to the maximum age. It cannot lag a
  change made through this platform, which reads as `refreshing` at once.
- A mentor who becomes bookable reads as `refreshing` until the next run.
- The `MentorFreeBusy` call now runs on a worker thread everywhere. That fixes
  `/slots` too, which previously blocked the event loop for the length of a
  Google round trip.
- This is the first stored derived value in the schema (D56 forbids them for
  counts). It is allowed because nothing reads it to decide anything; the
  moment something would, that reader must use live slots instead.
- `list_slots` re-reads a mentor's rules and bookings once per offering, and
  mentors are computed one after another. At today's few dozen a run takes
  seconds; bounded concurrency is the next step if a run nears the manifest's
  240s timeout.
- The job runs in staging only, like every other runtime job, until production
  scheduling is enabled.

### Confirmation

`tests/integration/test_mentor_next_available.py` pins each guarantee:
- the card matches the first slot `/slots` offers
- a booking, an hours change and a booking committed mid-refresh each read as
  `refreshing`
- a slot past its booking deadline is hidden, and the deadline is the slot less
  its notice
- age decides recomputation, and Google is read once per mentor
- the trigger covers exactly the nine tables and every column visibility reads;
  it ignores a no-op update, a headline edit and a past session being settled,
  and still fires on a listing change
- a dry run writes nothing

A mutation batch removing each guard turns a test red, including deleting every
change row rather than the snapshotted ones, and taking the snapshot after
computing rather than before.
