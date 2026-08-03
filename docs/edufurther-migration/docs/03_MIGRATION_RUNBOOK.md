# Migration Runbook

Operational procedure for moving ~6,700 rows out of Bubble.

---

## Core principles

**1. Three-stage pipeline. Never transform on read.**

```
Bubble Data API  →  staging schema (raw jsonb)  →  transform  →  public schema
```

When a transform turns out wrong — and one will — you re-run from staging, not
from Bubble's rate-limited API.

**2. `legacy_bubble_id` on every migrated table.** Non-negotiable. It makes the
ETL idempotent, makes reconciliation possible, and saves you when a mapping turns
out wrong three weeks in.

**3. Reconcile before advancing.** A phase isn't done when it loads without
error. It's done when the reconciliation script passes.

**4. Rehearse twice, timed.** That number decides whether you need a read-only
freeze or a dual-write period at cutover.

---

## Staging schema

```sql
CREATE SCHEMA staging;

CREATE TABLE staging.bubble_raw (
  id            bigserial PRIMARY KEY,
  table_name    text NOT NULL,
  bubble_id     text NOT NULL,
  payload       jsonb NOT NULL,
  extracted_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (table_name, bubble_id)
);

CREATE INDEX ON staging.bubble_raw (table_name);
CREATE INDEX ON staging.bubble_raw USING gin (payload jsonb_path_ops);
```

One table for everything. `ON CONFLICT (table_name, bubble_id) DO UPDATE` makes
extraction re-runnable.

### Extraction notes

- Bubble's Data API paginates with `cursor` and `limit` (max 100). Expect
  rate limiting — build in backoff from the start.
- Extract **everything** before transforming anything. You want a complete
  snapshot, not a moving target.
- Record the extraction timestamp per table. If a phase takes days, you need to
  know what's stale.
- **Private fields are not exposed by default.** Check API privacy rules in
  Bubble before assuming a field will come through — a silently missing column is
  worse than an error.

---

## Phase M0 — Foundation

**No user data.** Run `schema/00_foundation.sql`.

### Seeds required before M1

| Seed | Source | Rows |
|---|---|---|
| `countries` | ISO 3166-1 | ~250 |
| `languages` | SIL ISO 639-3 tab-delimited download, filtered to `Scope IN ('I','M')` and `Language_Type = 'L'` | ~7,000 |
| `degree_levels` | Inserted by the migration file | 6 |
| `service_offerings` | Hand-mapped from the two legacy option sets | ~20–30 |
| `scholarship_programs` | Hand-seeded starter set | ~40 |

**`service_offerings` is the important one.** It merges two previously separate
option sets — `Mentorship Goals` (mentee side) and `Mentor Services/Support`
(mentor side). Do this mapping by hand, deliberately, before M2. Get the union
right and matching works; get it wrong and you've rebuilt the original problem.

Set `is_featured` on ~40 languages based on your actual user base — English,
French, Portuguese, Arabic, Swahili, Yoruba, Igbo, Hausa, Nigerian Pidgin
(`pcm`), Amharic, Twi, Zulu, Shona, Wolof, Somali, Tigrinya, Oromo, Fula,
Kinyarwanda, Luganda, Chichewa, Xhosa, Afrikaans, plus the major European and
Asian languages.

---

## Phase M1 — Identity

**~2,100 rows. Critical path. Everything downstream depends on this.**

Order within the phase:
```
users
  → user_profiles
  → user_languages
  → auth_identities
  → user_onboarding
  → user_legal_consents
  → admin_users
  → calendar_connections
```

### Transform notes

**Email normalisation.** Lowercase, trim. Check for duplicates before loading —
`citext` + the partial unique index will reject them, and you want to find them
in staging, not mid-load.

```sql
SELECT lower(payload->>'email') AS email, count(*)
FROM staging.bubble_raw WHERE table_name = 'user'
GROUP BY 1 HAVING count(*) > 1;
```

**Timezone.** `UserTimezonID` may hold non-IANA values. Map to IANA; default to
`UTC` and flag for review rather than guessing.

**`Registration format` → `auth_identities`.** Only create a row for `google` or
`linkedin`. Email registrations get no identity row — they authenticate via
`auth_codes`.

**Phone numbers.** There are none. Leave `phone_e164` null across the board and
plan a collection campaign. WhatsApp reaches nobody whose number you don't have.

**Admin roles.** Only create `admin_users` rows where the legacy `Admin` option
set is populated. `granted_by` will be null for migrated rows — that's honest.

### Reconciliation

```sql
-- Row counts
SELECT 'users' AS t, count(*) FROM users
UNION ALL SELECT 'staging', count(*) FROM staging.bubble_raw WHERE table_name='user';

-- Every legacy row landed
SELECT s.bubble_id FROM staging.bubble_raw s
LEFT JOIN users u ON u.legacy_bubble_id = s.bubble_id
WHERE s.table_name = 'user' AND u.id IS NULL;

-- Null rates on fields that shouldn't be null
SELECT
  count(*) FILTER (WHERE email IS NULL)      AS null_email,
  count(*) FILTER (WHERE first_name IS NULL) AS null_first_name,
  count(*) FILTER (WHERE timezone = 'UTC')   AS defaulted_timezone
FROM users;

-- Profile coverage: 858 PersonalInfo rows against 1,200 users is EXPECTED,
-- not an error. Confirm the gap matches the legacy gap.
SELECT count(*) FROM user_profiles;
```

**Spot-check 20 random users** field by field against Bubble before advancing.

**Do not start M2 until M1 reconciles clean.**

---

## Phase M2 — Attributes

**~1,750 rows.** `education_entries`, `institutions`, `mentee_goals`,
`mentor_profiles`, `user_awards`, scholarship experience, service offerings.

### Institution matching

**First, measure the mess:**

```sql
SELECT payload->>'schoolName' AS school, count(*)
FROM staging.bubble_raw WHERE table_name = 'education'
GROUP BY 1 ORDER BY 2 DESC;
```

Hipolabs autocomplete is already in use in Bubble, so this may be cleaner than
expected. 200–400 distinct values across 940 rows means users selected cleanly.
600+ with obvious variants means free-typing got through.

**Matching procedure:**

1. Load the Hipolabs JSON into a temp table
2. Exact match on name → upsert `institutions`, link
3. Trigram match for the remainder:

```sql
SELECT e.school_name_raw, i.name, similarity(e.school_name_raw, i.name) AS score
FROM education_entries e
CROSS JOIN LATERAL (
  SELECT name, country_code FROM temp_hipolabs
  ORDER BY e.school_name_raw <-> name LIMIT 1
) i
WHERE e.institution_id IS NULL
ORDER BY score DESC;
```

4. Auto-link above 0.85
5. Manual review of the tail
6. Leave the rest with `institution_id` null — `school_name_raw` still displays,
   and they get linked opportunistically on next profile edit

**Capture `domain` for every matched institution.** It's the only stable natural
key; names change, domains rarely do.

### Degree goal mapping

Same approach on `Members Goals.degreeGoal`. Map "Masters" / "masters" / "MSc" /
"Master's Degree" → `degree_levels.slug = 'masters'`. Anything unmappable goes to
`mentee_goals.degree_goal_raw` rather than being dropped.

### `is_most_recent` integrity

The partial unique index enforces one per user. If legacy data has multiple,
resolve before load:

```sql
-- Find users with more than one most-recent degree
SELECT user_id, count(*) FROM education_entries
WHERE is_most_recent GROUP BY 1 HAVING count(*) > 1;
```

Resolution rule: keep the one with the latest `date_end`; if tied, latest
`date_start`; if still tied, arbitrary but deterministic (lowest
`legacy_bubble_id`).

### Mentor profiles

Sourced from `Mentor (front search)` (44 rows), but **only the 13 real fields**.
Everything else is derived or duplicated — see `02_FIELD_MAPPING.md` §11.

Map `approvedText` → `approval_status`, and set `listing_status` from
`availableStatus`. Mentors who are approved but were unavailable get
`listing_status = 'unlisted'`, `unlisted_reason = 'mentor_paused'`.

---

## Phase M3 — Availability

**~200 rows. Highest-risk transform.**

Legacy stored pre-formatted local time strings alongside actual times. Getting
this wrong silently produces wrong booking slots, and DST breaks it twice a year.

### Transform

```
dayOfWeekIn        → day_of_week (0-6, confirm Bubble's convention: 0=Sunday?)
startTime          → start_time (time, wall-clock)
endTime            → end_time (time, wall-clock)
timeZone           → timezone (IANA)
availableDay-Bool  → is_active
```

**Ignore all four TXT columns entirely.** They're display formatting.

### Validation — do this before cutover

```sql
-- Windows must be ordered
SELECT * FROM availability_rules WHERE end_time <= start_time;

-- Non-IANA timezones
SELECT DISTINCT timezone FROM availability_rules
WHERE timezone NOT IN (SELECT name FROM pg_timezone_names);

-- Reconstruct a known mentor's next 7 days of slots and compare against what
-- Bubble currently shows for the same mentor. Do this for at least 5 mentors
-- across at least 3 different timezones.
```

**Pick a mentor in a DST-observing timezone** (Europe/London, America/Toronto)
and verify slots across a DST boundary. This is where the bug will be if there
is one.

### CalendarExtra → availability_exceptions

5 rows. Convert `block-Date(s)` list into `daterange` values. Also migrate
`unavailableDateRange` and `unavailableDuration` from the front-search table here.

---

## Phase M4 — Sessions

**~2,000 rows. Largest and most complex.**

### Pre-transform reconciliation — do this first

```sql
-- The 138-row gap: is it all cancelled bookings with no tracker?
SELECT
  b.payload->>'sessionStatus' AS status,
  count(*) AS bookings,
  count(t.bubble_id) AS with_tracker
FROM staging.bubble_raw b
LEFT JOIN staging.bubble_raw t
  ON t.table_name = 'sessiontracker'
 AND t.payload->>'SessionID' = b.bubble_id
WHERE b.table_name = 'sessionbooking'
GROUP BY 1;

-- Did any booking produce MULTIPLE trackers? (reschedules)
SELECT payload->>'SessionID' AS booking_id, count(*)
FROM staging.bubble_raw WHERE table_name = 'sessiontracker'
GROUP BY 1 HAVING count(*) > 1;
```

The answers determine your merge strategy. Don't write the transform first.

### Order within the phase

```
1. Auto-create "General Mentorship" session_type per mentor (44 rows)
   + session_type_booking_configs with the mentor's legacy meetingDuration
2. sessions (merged from bookings + trackers)
3. session_participants (2 rows per session)
4. session_events (synthesised — see below)
```

### The exclusion constraint will reject overlaps

Legacy data almost certainly contains double-bookings that Bubble allowed. Find
them before loading:

```sql
-- Run against staging, pre-load
SELECT a.bubble_id, b.bubble_id, a.payload->>'Mentor'
FROM staging.bubble_raw a
JOIN staging.bubble_raw b
  ON a.payload->>'Mentor' = b.payload->>'Mentor'
 AND a.bubble_id < b.bubble_id
 AND tstzrange((a.payload->>'SessionDateTime-UTC')::timestamptz,
               (a.payload->>'SessionDateTime-UTC')::timestamptz
               + ((a.payload->>'Duration')::int || ' minutes')::interval)
     && tstzrange((b.payload->>'SessionDateTime-UTC')::timestamptz,
               (b.payload->>'SessionDateTime-UTC')::timestamptz
               + ((b.payload->>'Duration')::int || ' minutes')::interval)
WHERE a.table_name = 'sessionbooking' AND b.table_name = 'sessionbooking';
```

**Resolution:** historical overlaps between two *completed* sessions are data you
can't change. Options: (a) load them with status `completed`, which the
constraint's `WHERE` clause excludes anyway, or (b) if genuinely conflicting
active sessions exist, resolve manually. The constraint only applies to
`pending_mentor_approval` and `confirmed`, so most legacy data passes.

### Synthesising `session_events`

Legacy has no event history. Create a minimal, honest reconstruction:

| Condition | Events created |
|---|---|
| Every session | `NULL → pending_mentor_approval` at `created_at` |
| `bookingRequestAccepted` true | `pending_mentor_approval → confirmed` |
| `status = 'completed'` | `confirmed → completed` |
| `SessionCancel = true` | `→ cancelled`, `actor_id` from `Canceled By`, `reason_text` from the cancel message, `reason_code = 'admin_action'` (unknowable from legacy data) |
| `Expiration` set | `→ expired`, `actor_id` null |

**Set `metadata = '{"synthesised": true}'`** on all migrated events. Future you
needs to know which events are reconstructed versus recorded. Timestamps will be
approximate — that's acceptable and honest, as long as it's marked.

### Session participants

Two rows per session, created in the same transform. Map `Last Joined(mentee)` →
`joined_at` on the mentee row, `TrackStatus(mentee)` → `attendance_status`, and
the same for mentor.

---

## Phase M5 — Reviews and opening credits

**~70 rows plus one lot per active mentee.**

**Reviews:** straight mapping. Link `session_id` where derivable by matching
`reviewedBy` + `reviewedFor` + proximity to a completed session. Where it isn't
derivable, leave null — the unique index is partial.

**Credits:** create ONE `credit_lots` row per user with a non-zero
`bookingCredit`:

```
source            = 'monthly_free'
quantity_granted  = bookingCredit
quantity_remaining = bookingCredit
expires_at        = bookingCreditRenewDate (or end of current month)
```

Plus one `credit_transactions` row with `reason = 'grant'`.

**You cannot reconstruct transaction history** — Bubble never recorded it. Only
opening balances. Note this in the migration log so nobody later wonders why
credit history starts abruptly.

---

## Phase M6 — Communications

**~750 rows. Low risk, no downstream dependencies.**

**Notifications:** map `Session` and `Review` to the typed FKs. Everything else
into `context` jsonb. Create `notification_recipients` rows from the
`Receiver` / `Receiver(list of users)` fields, with `read_at` set where the user
appears in `Seen(list of users)`.

**Messaging:** 13 conversations, 44 messages. Nearly free to rebuild properly.
Create `conversation_participants` from `sendBy` + `receiveBy` on the starter.
Set `last_read_at` from `seenRead` where available.

---

## Cross-cutting: file migration

**Start this on day one.** Network-bound, slow, and independent of schema.

Files currently on Bubble's S3:
- `User.User Profile image` (~1,200)
- `PersonalInfo.Profile banner Image`
- `Mentor (front search).pictureProfile` (duplicate of avatar)
- `VB-Vision Boards.visionBoardCardShareImg`, `.visionBoardCertShareImg`

```
1. Extract all URLs from staging
2. Download with concurrency limits and retry
3. Upload to your storage with randomised keys
4. Record old URL → new key in a mapping table
5. Rewrite URLs during the relevant load phase
6. Verify: zero rows where avatar_url LIKE '%bubble%'
```

**Verification query before cutover:**

```sql
SELECT 'avatar' AS field, count(*) FROM user_profiles WHERE avatar_url LIKE '%bubble%'
UNION ALL
SELECT 'banner', count(*) FROM user_profiles WHERE banner_url LIKE '%bubble%'
UNION ALL
SELECT 'vb_card', count(*) FROM vision_boards WHERE card_share_image_url LIKE '%bubble%';
```

All three must return zero.

---

## Standard reconciliation template

Run after every phase, every rehearsal:

```sql
-- 1. Row counts match
-- 2. Every legacy row has a destination
SELECT s.table_name, s.bubble_id
FROM staging.bubble_raw s
LEFT JOIN <target> t ON t.legacy_bubble_id = s.bubble_id
WHERE s.table_name = '<legacy_table>' AND t.id IS NULL;

-- 3. No orphaned FKs (should be impossible, but verify)
-- 4. Null rates on required fields
-- 5. Spot-check 20 random records field by field
```

**Automate 1–4. Do 5 by hand.** The hand check is what catches transform logic
that's technically valid and semantically wrong.

---

## Cutover

### Rehearsal (twice, minimum)

1. Fresh staging database
2. Full extract → all phases → all reconciliations
3. **Time each phase.** Record it.
4. Smoke test the application against migrated data
5. Tear down, repeat

The total time decides your strategy:

| Total | Strategy |
|---|---|
| < 2 hours | Read-only freeze, migrate, switch DNS |
| 2–8 hours | Overnight window, announce in advance |
| > 8 hours | Dual-write period, or phase the cutover by feature |

### Cutover day

```
1.  Announce read-only mode in the Bubble app
2.  Final extract (delta since last full extract)
3.  Run all phases
4.  Run all reconciliations — ABORT if any fail
5.  Verify zero Bubble URLs remain
6.  Smoke test: login, browse mentors, view a session, send a message
7.  Switch DNS / feature flag to the new app
8.  Keep Bubble read-only for 30 days
9.  Monitor error rates and slow queries for 48 hours
```

### Rollback

Keep the Bubble app **read-only, not deleted**, for at least 30 days. If
something surfaces on day 3, you need the source of truth intact.

The new database keeps `legacy_bubble_id` permanently — that's your join back to
Bubble for any post-cutover investigation.

---

## Post-cutover

**Immediate:**
- Phone number collection campaign (WhatsApp needs it)
- `current_country_code` prompt on next profile edit
- Retention jobs: `auth_codes` > 90 days, `idempotency_keys` > 24h,
  `booking_attempts` > 1 year

**First month:**
- Review the `institutions` pending queue, merge duplicates
- Review `scholarship_programs` pending queue
- Baseline the reporting views — especially `v_review_coverage` (expect ~5.7%)

**Then archive:** export the full Bubble dataset to cold storage before deleting
anything.
