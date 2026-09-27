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

- **Composio is gone** (ADR 0012's follow-up; Google is called directly).
  Google's own documentation states that standard Calendar API use costs
  nothing, with 1,000,000 requests a day per project and 600 a minute per user.
  At 50 mentors every five minutes this job makes 14,400 calls a day, 1.4% of
  that. At 2,000 mentors it makes 576,000. Google says it will begin charging
  above the daily threshold later in 2026; that is the number to watch.
- **Freshness is kept where it decides anything.** Booking still reads live
  slots. The stored value decides nothing: it is a display hint, and it is
  withheld whenever it cannot be vouched for (below).

Nango, an open-source integration layer, is under consideration as a future
way to reach Google. If it is adopted, re-price this job against it.

## Decision

1. **`mentor_next_availability`** stores one row per bookable mentor:
   `next_available_at` (the first instant any live offering could be booked,
   from the mentor's own today over `MAX_PROJECTION_DAYS`), `changed_at`,
   `seen_changed_at` and `computed_at`. The time comes
   from `list_slots`, the function booking uses, so the card and the booking
   flow cannot disagree about what is free.
2. **`trg_mark_next_available_stale`** sits on the eight tables that decide when
   a mentor is free: `sessions`, `availability_rules`,
   `availability_exceptions`, `session_types`, `session_type_booking_configs`,
   `session_type_scheduling_windows`, `mentor_profiles` and
   `calendar_connections`. Any change sets the mentor's `changed_at`,
   creating the row if the job has not reached that mentor yet. An `UPDATE`
   that changes nothing but `updated_at` marks nothing, and the two tables with
   frequent unrelated writes fire only on the columns that decide availability.
   A trigger rather than application hooks, because a hook missed in one of
   many write paths is silent. A test pins the list against `pg_trigger`.
3. **The card vouches or says nothing.** A refresh reads `changed_at` before it
   computes and writes that value back as `seen_changed_at`; the card shows the
   time only while `seen_changed_at = changed_at` and the time is still ahead.
   **An equality, not two clocks compared**: review of the first version found
   that comparing the refresh's start time against `changed_at` blessed a taken
   slot when a booking's transaction committed after the refresh read the data,
   and whenever the app host's clock ran ahead of the database's. Both break the
   equality. So a booking, an hours change, or a change landing mid-refresh
   reads as `refreshing` until the next run — never a time already taken.
   `next_available_state` tells the client which of `open`, `none` and
   `refreshing` a null means.
4. **A QStash job, `refresh-next-available`, every five minutes** (the manifest
   cron, overridable per environment like every other job). It recomputes rows
   that are stale, or older than `NEXT_AVAILABLE_MAX_AGE_MINUTES` (default 5,
   compared with a minute's tolerance for delivery jitter, so the default means
   every run).
   Age is the only thing that catches a change made in Google, which fires no
   trigger. Five minutes because QStash's free tier allows 1,000 messages a
   day: every five minutes is 288, every minute would be 1,440.
5. **One free/busy call per mentor per refresh**, shared across their
   offerings. **Only intervals are read, and nothing from Google is stored**:
   the row holds one derived instant, so "never mirrored" still holds.
6. **No lock spans a Google call, and a timeout keeps what it finished.** Each
   mentor is computed, then written and committed on its own, so a mentee's
   booking — whose trigger writes the same row — never waits on Google, and a
   run QStash times out has still saved every mentor it reached. One free/busy
   builder serves both this job and the API's slot reads.

## Consequences

- A card can lag a Google-side change by up to the maximum age. It cannot lag a
  change made through this platform, which reads as `refreshing` immediately.
- A mentor who becomes bookable reads as `refreshing` until the next run.
- `list_slots` re-reads a mentor's rules and bookings once per offering, and
  mentors are computed one after another. At today's few dozen a run takes
  seconds; bounded concurrency is the next step if a run nears the manifest's
  240s timeout.
- This is the first stored derived value in the schema (D56 forbids them for
  counts). It is allowed because nothing reads it to decide anything; the
  moment something would, that reader must use live slots instead.
- The job runs in staging only, like every other runtime job, until production
  scheduling is enabled.

### Confirmation

`tests/integration/test_mentor_next_available.py` pins each guarantee: the card
matches the first slot `/slots` offers; a booking, an hours change and a change
mid-refresh each read as `refreshing`; a past time is hidden; age decides
recomputation; Google is read once per mentor; the trigger covers exactly the
eight tables, ignores a no-op update and a bio edit, and still fires on a
listing change; and a dry run writes nothing. A mutation batch removing each
guard turns a test red, including writing `changed_at` as read at write time
rather than as read before computing — which is the race the equality closes.
