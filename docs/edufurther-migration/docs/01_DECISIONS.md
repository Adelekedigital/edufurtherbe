# Decisions Log

Every significant decision, why it was made, and what was rejected. Read this
before changing anything in the schema — most of these look arbitrary until you
know what they're preventing.

Decisions marked **REVISED** changed during design; the original reasoning and
why it was wrong are both recorded.

---

## D1 — Split `User` into 9 tables

**Decision.** `users` holds identity and auth only. Profile, OAuth, onboarding,
credits, admin role, and languages all move out.

**Why.** One table was doing five jobs with ~30 columns. Every profile edit
touched the same row as every login. Adding an OAuth provider meant a migration
on the busiest table.

**Result:** `users`, `user_profiles`, `auth_identities`, `auth_codes`,
`user_onboarding`, `user_legal_consents`, `admin_users`, `user_languages`,
`calendar_connections`.

---

## D2 — Authorization from profile existence, not a `role` column

**Decision.** `users.primary_role` is a UX hint. Permissions check whether a
`mentor_profiles` or `mentee_goals` row exists.

**Why.** The `role` column was already redundant with the profile tables — a user
with `role = 'mentee'` and a mentor profile is a data-integrity question with no
right answer. Profile existence can't disagree with itself.

**Consequence.** Dual roles work from day one at zero cost. No `user_roles`
junction needed.

**Cost of the alternative** (checking `role` throughout, then retrofitting):
data migration is trivial, but every role check scattered across the codebase
needs rewriting, plus RLS policies and a breaking API change where `role` becomes
an array. One to two weeks, entirely avoidable.

**Product questions this surfaces** (not schema, but they'll come up):
- Can a mentor book themselves? `CHECK (mentor_id <> mentee_id)` added regardless.
- Does a mentor-who-is-a-mentee get free monthly credits? Closes a farming hole.
- Separate reputations? `reviews.reviewed_role` added so mentor and mentee
  behaviour don't blend into one score.

---

## D3 — Merge `SessionBooking` + `SessionTracker`

**Decision.** One `sessions` table.

**Why.** 1,073 vs 935 rows, both carrying `google/dailyMeetingVenue` and
`google/dailyRoomName`. That's a Bubble workaround, not a domain distinction.

**Before writing the transform**, verify:
- Is the ~138-row gap entirely cancelled bookings with no tracker?
- Did any booking produce multiple trackers (reschedules)?

---

## D4 — Keep `mentor_id` and `mentee_id` on `sessions`

**Decision.** Both stay as columns. `session_participants` handles attendance only.

**The alternative considered:** move both into `session_participants` with a
`role`, derive everything. Good normalization instinct. Two things block it:

**1. The exclusion constraint needs `mentor_id` on the row.**

```sql
EXCLUDE USING gist (
  mentor_id WITH =,
  tstzrange(starts_at, starts_at + (duration_minutes||' minutes')::interval) WITH &&
) WHERE (status IN ('pending_mentor_approval','confirmed'))
```

`EXCLUDE` operates on columns of a single table; it cannot reference a joined
table. Move `mentor_id` out and you lose database-level double-booking
prevention — the exact bug class Bubble couldn't prevent and this migration
exists to escape. The alternative is a `BEFORE INSERT` trigger doing an overlap
query, which is race-prone without additional locking and is application logic
pretending to be a constraint.

**2. RLS gets expensive.**

```sql
-- with columns
USING (mentee_id = auth.uid() OR mentor_id = auth.uid())

-- participants-only
USING (EXISTS (SELECT 1 FROM session_participants
               WHERE session_id = sessions.id AND user_id = auth.uid()))
```

The second runs per row on every query touching the most-read table.

**The framing:** these aren't denormalization — they express a **domain
invariant**. Sessions are 1:1 between exactly one mentor and one mentee, by
design. Encoding that is correct.

**Migration path if the invariant changes** (cohort sessions, group workshops):
keep `mentor_id` as the host, drop `mentee_id`, use participants for attendees.

**Guard against drift:**
```sql
CREATE UNIQUE INDEX ON session_participants (session_id) WHERE role = 'mentor';
```
Participant rows are created in the same transaction as the session insert.

---

## D5 — Session status is NOT derived from attendance

**Decision.** `sessions.status` is an explicit column.

**Why.** Status is a lifecycle state that exists **before anyone attends**. A
session is `pending_mentor_approval` at creation and `cancelled` if called off —
in both cases there is no attendance data to derive from.

The relationship is one-directional:

```
attendance INFORMS:         confirmed → completed | no_show
attendance DOES NOT DEFINE: pending | confirmed | cancelled | declined | expired
```

A job runs after `starts_at + duration`, reads participant attendance, writes one
`session_events` transition.

---

## D6 — `session_events` with `reason_code` AND `reason_text`

**Decision.** Immutable transition log replacing `SessionCancel(Y/N)`,
`Canceled By`, `Session Cancel/Decline Message`, `bookingRequestAccepted`,
`Expiration`.

**Two different fields, deliberately:**
- `reason_text` — free text from the human. "Sorry, conference clash."
- `reason_code` — enum you `GROUP BY`. Drives automated policy.

**Why the code exists.** "What percentage of mentor-side cancellations are
scheduling conflicts" decides whether you build reschedule flows. Free text can't
answer it without someone reading 200 rows. And refund rules run off the code:
`mentor_unavailable` → refund; `mentee_no_longer_needed` within 24h → don't.

**What this unlocks that was impossible before:** "how many sessions were
cancelled by mentors within 24 hours of start." Today `Canceled By` has no
timestamp, so it's unanswerable.

`actor_id` is nullable — null means the system did it. More honest than inventing
a system user.

---

## D7 — Credit lots, not a counter or period balance **[REVISED]**

**Original design:** `credit_periods` with a monthly reset.

**Why it was wrong:** it assumed all credits are free. The platform is moving to
paid alongside free, and **paid credits must never expire** — expiring something
someone bought is a chargeback and, in several jurisdictions, unlawful. Free
credits expire monthly; purchased ones don't. A single balance can't represent
both lifecycles.

**Decision.** `credit_lots` — a batch with one origin and one expiry.

Consumption is FIFO with expiring-first ordering:
```sql
ORDER BY (expires_at IS NULL), expires_at ASC, granted_at ASC
```
Burn free before paid, soonest-expiring first. What users expect, minimises
liability.

"New credit clears old, stands at 5" needs no reset logic — last month's lot
expired, this month's granted 5.

---

## D8 — `credit_transactions` is not optional

**Asked directly: why not just a balance?** Four reasons:

1. **Refunds.** Book (−1), mentor cancels, credit returns (+1). With a counter
   there's no record. "I was charged for a session that never ran" is
   unanswerable.
2. **Concurrency.** `UPDATE credits = credits - 1` under two simultaneous
   bookings is a lost-update race.
3. **Abuse detection.** "5 credits in 8 minutes across 5 mentors" is a query
   against transactions. Against a counter it's invisible.
4. **Money.** Once credits are purchasable this is accounting — you must
   reconcile Stripe against internal balances.

**Migration limit:** only opening-balance entries can be created. Bubble never
recorded transaction history.

---

## D9 — Referral gate

**Decided:**

| Trigger | Lot |
|---|---|
| Signup, no invite | `signup_baseline`, qty 1, never expires, never renews |
| First qualifying invite | `referral_unlock`, qty 5, expires end of month |
| Every month after | `monthly_free`, qty 5, expires end of month |

`referral_unlocks` is one row per user — the gate is once-only, and the PK makes
double-unlocking structurally impossible.

**Still open:** what counts as "qualifying." Email-verified alone is farmable
with disposable addresses. Recommendation: invitee completes onboarding.

**Instrument the funnel:** signup → baseline credit → first session → invite →
unlock. If people burn the baseline and never invite, the prompt is mistimed —
and you'll only know if the events exist.

---

## D10 — Booking limits are data, enforcement is advisory-locked

**Decision.** `booking_policies` table, resolved user → role → global.

**Why data:** these numbers get tuned monthly once real behaviour shows up.

**Enforcement must be race-safe.** Counting then inserting in separate statements
is a check-time-of-use bug — a double-click gets three sessions past a limit of
two:

```sql
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('booking:' || :mentee_id));
-- count upcoming, reject if >= max_concurrent_upcoming
INSERT INTO sessions (...);
COMMIT;
```

The lock serialises one user's bookings without touching anyone else's
throughput.

**`booking_attempts` records rejections.** "How many bookings did we block last
month, and why" tells you whether a limit of 2 protects mentor capacity or
strangles engagement. Without it, a too-tight limit looks identical to low demand.

**The concurrency cap and credit balance are independent gates.** A mentee with 5
credits can still be blocked at 2 upcoming sessions.

---

## D11 — Penalties decay; reports are adjudicated

**Decision.** `user_infractions` with `expires_at`; `user_standing` as a
nightly-recomputed summary.

**Why decay:** a no-show in January shouldn't cost credits in September. Minor
90d, major 180d, severe never.

**Why standing is materialised:** the monthly grant job does one indexed lookup
instead of an aggregate over infractions for 1,200 users.

**Reports must be adjudicated, not auto-applied.** `status = 'upheld'` creates
the infraction; a human decides. Otherwise one annoyed mentor revokes someone's
credits.

**Blocks are a private preference, not an accusation.** A mentor blocking a
mentee shouldn't create an infraction until N independent mentors block the same
person.

**Appeals need a path.** `waived_at` / `waived_by` / `waiver_reason`. People have
genuine emergencies.

**Warn before penalising.** `status = 'warned'` triggers a notification. Silent
credit withholding causes churn.

---

## D12 — One shared `service_offerings` vocabulary

**Decision.** `mentor_service_offerings` and `mentee_goal_needs` reference the
same lookup.

**Why matching fails today:** Bubble stored "Mentorship Goals" (mentee) and
"Mentor Services/Support" (mentor) as two **separate option sets with no
mapping**. "Does this mentor do what this mentee needs" required a
hand-maintained mapping that never existed.

Now it's a join, before any AI:

```sql
SELECT m.mentor_user_id, COUNT(*) AS overlap
FROM mentee_goal_needs g
JOIN mentor_service_offerings m USING (service_offering_id)
WHERE g.user_id = :mentee
GROUP BY 1 ORDER BY overlap DESC;
```

This table is also the seed for `tags` when AI matching is built.

---

## D13 — Scholarship data is user-level **[REVISED]**

**Original design:** `mentor_scholarship_experience` under mentor services.

**Why it was wrong:** two different things were conflated.

- **A credential** — "I won a Chevening scholarship." A fact about a person. Any
  user can have it, including a mentee who won something and is now seeking a
  second degree.
- **A capability** — "I can advise on Commonwealth applications." Mentor-side.

Correlated but not identical: someone can advise on scholarships they never won,
and a mentee can hold awards without offering anything.

**Decision.** `user_scholarship_experience` at user level, with `relationship`
(`awarded` | `applied` | `advised`) doing the work. Mentor search filters on
`advised`; a mentee profile displays `awarded`. `user_awards` (formerly
`mentor_awards`) also moves to user level.

---

## D14 — `scholarship_program_id` is NOT NULL, with a merge path **[REVISED]**

**Original design:** nullable FK plus `custom_program_name` free text.

**Why it changed:** always creating a row means one code path, no branching on
"is this custom or canonical," and simpler queries.

**The cost:** you now need a merge mechanism, and it's mandatory not optional.
Without it you get "Chevening", "chevening scholarship", "Chevening Award" as
three rows within a month and filtering stops working.

1. **Suggest before create** — trigram match on input, "did you mean Chevening?"
   Catches most duplicates at the source.
2. **Admin merge** — set `merged_into_id`, repoint references, `status='merged'`.
   Keep the merged row so cached client references still resolve.

`usage_count` is the admin work queue signal: pending + 8 users → approve;
pending + 1 user + a typo → merge.

Same pattern on `institutions`.

---

## D15 — `institutions` is a registry, not a mirror **[REVISED]**

**Original suggestion:** seed from ROR, thousands of rows.

**Pushback was right** — mirroring a catalogue you can query live is pointless.

**But the string alone doesn't work either.** If you persist only "University of
Lagos", you're back to 940 rows of free text with four spellings of Unilag and no
way to derive country or filter by institution.

**Decision.** Populate on demand:

```
1. User types → Hipolabs autocomplete (client-side, no storage)
2. User selects → API returns { name, country, domain, web_pages }
3. Backend upserts into `institutions` on `domain` (natural key)
4. education_entries.institution_id → that row
```

**Expect ~200–400 rows from 940 education entries**, not 9,000. A registry of
what's referenced, not a mirror.

**Why a table rather than a denormalised string:**
- Country derives once at write, not via API call on every profile render
- Hipolabs is a static GitHub JSON file, not a versioned API with an SLA;
  historical data shouldn't depend on that repo staying up
- "Mentors who studied in the UK" is a join, not a runtime API fan-out
- FK integrity
- Hipolabs is incomplete for African institutions; `source='manual'` fills gaps

**Always keep `school_name_raw`.** Nullable `institution_id` means unmatched
entries still display and can be linked later.

---

## D16 — Three countries, and study country moves to education

**Decision.**

| Country | Home | Meaning |
|---|---|---|
| `origin_country_code` | `user_profiles` | Nationality / where from |
| `current_country_code` | `user_profiles` | Where they live now — **NEW, absent from Bubble** |
| `country_code` | `education_entries` (via institution) | Where **this degree** was earned |
| `country_code` | `mentee_goal_countries` | Target, aspirational |

**Why `current_country_code` matters:** a Nigerian mentee living in the UK has
different visa questions, timezone, and scholarship eligibility than one in
Lagos. Starts null; collect at next profile edit.

**Why study country moves to `education_entries`:** a mentor with degrees from
Nigeria, then the UK, then Canada was flattened to one `Country of study(mentor)`
value. That's lossy, and it's exactly what "who studied in Canada" needs to
search across. `mentor_profiles.primary_study_country_code` stays as a
display/filter convenience.

---

## D17 — ISO 639-3 for languages, not 639-1

**Decision.** Three-letter codes, ~7,000 living languages, in a lookup table.

**Why not 639-1:** the two-letter set covers ~184 languages and **omits Nigerian
Pidgin entirely**. For an African-focused platform that's a significant gap. Also
missing: Twi, several Tigrinya variants, and a long tail.

**Source:** SIL publishes the complete code set as tab-delimited UTF-8, free, no
API key — `iso639-3.sil.org/code_tables/download_tables`. Filter to
`Scope IN ('I','M')` and `Language_Type = 'L'`. Commit the result as a versioned
migration; re-check annually.

**`is_featured`** matters practically — a mentee in Lagos shouldn't scroll past
7,000 options to find Yoruba. Seed the featured set from your actual user base.

**`code_639_1` retained** for `hreflang` and browser locale hints where a
two-letter code is required, without losing languages that lack one.

**Macrolanguage note:** for "what languages do you speak," the macrolanguage
(`ara`) is the right granularity — nobody wants to choose between 30 Arabic
variants in a dropdown.

**Attached to `users`, not `mentor_profiles`** — mentee language matters for
matching too.

---

## D18 — Availability: rules + exceptions, slots computed

**Decision.** `availability_rules` (recurring weekly, wall-clock + IANA zone) and
`availability_exceptions` (blocks and overrides, `daterange`). Slots never stored.

**Twelve legacy columns → six.** Four were the same fact in different display
formats.

**Time storage rule:**
- Wall-clock time + IANA zone → for **rules** (recurring, DST-aware)
- `timestamptz` → for **instants** (a specific session)

Storing a pre-formatted local string for a recurring rule is what breaks across
DST — twice a year, silently.

**Multiple rows per day** handles split availability (morning + afternoon with a
lunch gap), which the legacy one-row-per-day structure couldn't represent.

**Duration removed from availability entirely.** Availability defines *when*;
session types define *how long*. Two sources of truth for duration is a bug
waiting to happen.

---

## D19 — Delete the front-search table, index instead

**Decision.** No denormalised search table, no Typesense, no Meilisearch.

**Why the original existed:** avoiding a 6-way join on every search. Correct
instinct.

**Why it failed:** 44 rows manually synced against 31 mentor services and 192
availability rules means the data is already inconsistent somewhere. Counters
(`countCompletedSession`, `countReviewReceived`,
`percentageOfCompletedSession`) all derivable.

**Why a join is fine:** at 44 mentors every table fits in shared buffers with
room to spare. The planner hash-joins the whole thing. You'd need **~10,000+
mentors** before a well-indexed join needs help.

**What indexes cost:** write amplification (invisible at ~1,000 sessions/year),
disk (tens of KB), and HOT-update blocking (only matters on hot-path counters,
which you shouldn't store anyway).

**Escalation path when p95 crosses ~200ms:** `MATERIALIZED VIEW` with
`REFRESH CONCURRENTLY`. One-line change, no application rewrite, no vendor.

---

## D20 — `listing_status` binary, availability derived **[REVISED]**

**Original design:** `is_available` + `is_listed` booleans, plus a three-state
listed/paused/hidden model.

**Two problems:**
1. `is_available` was **derivable** from availability rules, exceptions, and
   existing sessions. Storing it recreates exactly the drift that made the
   front-search table untrustworthy.
2. "Paused but visible" conflated **searchability** (a profile attribute) with
   **profile-page access** (a rule about the *viewer*).

**Decision.** One field: `listing_status` (`listed` | `unlisted`) plus an
internal `unlisted_reason`.

Profile access is a rule, not a column:
```
render IF listing_status = 'listed'
       OR viewer has a session with this mentor (any status)
       OR viewer is admin
```

A mentee with a completed session needs the profile to render regardless of
listing state, or their history breaks and past reviews 404.

**Re-engagement without notification fatigue:** paused mentors see a **dashboard
stat** on login — "12 mentees searched for someone matching your profile while
you were paused." Zero notification cost, lands when they're already thinking
about the platform. `search_impressions_suppressed` captures it, and doubles as a
real product metric: how much demand goes unmet because mentors are paused?

---

## D21 — Meeting defaults cascade

| Field | Home | Rule |
|---|---|---|
| Duration | `session_type_booking_configs` only | Single source of truth |
| Venue | `mentor_profiles` default, session type overrides | `COALESCE(type.venue, mentor.default_venue)` |
| Meeting URL | Generated per session | Static room links only for `custom` venue |

**On static meeting URLs:** a personal room link (`meet.google.com/abc-defg`)
means back-to-back sessions share a room and an early joiner walks into the
previous session. That's a **privacy incident**, not a UX annoyance. Per-session
links are generated at confirmation.

**Migration:** every mentor gets an auto-created "General Mentorship" session
type in M4, so all 1,073 legacy sessions carry a `session_type_id`. That lets the
column become NOT NULL after migration — exactly one code path.

---

## D22 — Notifications: hybrid typed FKs + jsonb **[REVISED]**

**Original design:** typed FK per entity type.

**Pushback was right** — adding a nullable column per notification type means a
migration on a growing table every time you add a type, plus a table of mostly
nulls.

**Decision — the rule:**

> Add a typed FK column **only** when you need one of two things:
> 1. **Cascade/cleanup** — entity deleted or hidden, related notifications must
>    be found and handled
> 2. **Bulk join for list rendering** — the inbox query joins to it per row
>
> Everything else goes in `context` jsonb and gets **no column**.

Three qualify: `session_id`, `review_id`, `conversation_id`.

Credit grants, referral qualifications, mentor approvals, penalty warnings,
milestone completions — no columns. Terminal notifications; nothing needs to find
them later.

**Platform-wide announcements:** all three FKs null,
`type = 'platform_announcement'`, content in `context`. Normal case.

**Adding a fourth later is a cheap migration** (nullable column, no backfill).
Not forbidden — just rare and justified by rule 1 or 2.

**The discipline that keeps this honest:** `context` is read by **templates**,
never by business logic. The moment a rule branches on a `context` value, that
field has earned a real column.

---

## D23 — Own the notification record, rent the delivery

**Decision.** `notifications` + `notification_recipients` in your database;
`notification_outbox` dispatches to channels.

```
1. Business logic writes → notifications + recipients
2. Same transaction     → outbox rows
3. Dispatcher polls     → fans out → marks delivered_at
```

Business logic never calls a vendor SDK. Swapping providers touches one module.

**Not using Novu this phase.** 681 notifications doesn't justify it. Add it when
digesting and preference management are actually wanted.

---

## D24 — iOS PWA push: badging works, delivery receipts don't

**Corrected from an earlier claim.** The Badging API **does** work on iOS 16.4+
for home-screen web apps, and it's exposed in Worker contexts — so the service
worker's push handler can call `navigator.setAppBadge()`. Badge display follows
notification permission.

**Still required:** home-screen install. Push does not work in a Safari tab, and
iOS gives no `beforeinstallprompt` — you must teach Share → Add to Home Screen
manually. Expect drop-off. Permission request must come from a user gesture.

**Still unavailable:** delivery receipts (a 201 from Apple's gateway means
accepted, not displayed), silent push.

**Known issue:** subscriptions go inactive after prolonged inactivity. Handle
`410 Gone` by marking dead, and **re-subscribe on every app open**, not just at
first permission grant.

**Consequence:** email and WhatsApp must be **real channels**, not fallback
afterthoughts. For a booking confirmation, email is the reliable path; push is
the enhancement. This is exactly why the outbox pattern matters.

---

## D25 — WhatsApp: templates are the binding constraint

**Decision.** Meta Cloud API directly. `whatsapp_templates` registry.

**The structural constraint is not cost — it's that business-initiated messages
require pre-approved templates.** You cannot send arbitrary text. Submit
"Your session with {{1}} starts in {{2}}", wait for review, then fill variables.

**Category is enforced by Meta, not you.** Booking notifications are `utility`
(cheap tier). Any promotional element reclassifies to `marketing` at roughly 10×.
"Your session is confirmed — invite 3 friends for free credits!" gets
reclassified. **Keep referral prompts out of utility templates.**

**Provider:** Cloud API directly at this volume; BSP markup buys nothing yet.
Start Meta Business verification early (days to weeks).

**Note on OTP:** Nigeria is one of nine markets with a higher
authentication-international rate when the WABA is registered elsewhere. If most
OTPs go to Nigerian numbers, price out registering there.

---

## D26 — OTP over magic link

**Decision.** 6-digit codes, `auth_codes` table. Replaces `password_resets`.

**Why OTP wins here:**
- **No email-client prefetch problem.** Outlook Safe Links follows URLs in emails
  before the human clicks, consuming magic-link tokens. This is the single most
  common magic-link bug in production.
- **No WhatsApp in-app browser mismatch.** Tapping a link in WhatsApp opens its
  in-app browser — a different session from the user's real browser. The login
  lands in the wrong place.

**The trade-off: 6 digits is brute-forceable.** A million combinations isn't many
when scripted. These are not optional:

1. **Hash the code** (SHA-256). A DB read must not grant login.
2. **Lock after `max_attempts`** — invalidate the **code**, not just reject the
   guess. Otherwise unlimited tries against a live code.
3. **Invalidate previous codes on new request.** 100 live codes against a 1M
   space shifts the odds. One active code per (user, purpose) — enforced by a
   partial unique index.
4. **10-minute expiry.** Shorter than a magic link's 15.
5. **Rate limit per destination** (3 per 15 min), not per IP — per-IP is evadable
   and punishes shared connections, which matters in these markets.
6. **Constant-time comparison.**
7. **Uniform response and timing** regardless of account existence.
8. **CSPRNG generation.**

Verify and consume are separate atomic statements — see `01_identity.sql`.

**`password_hash` stays nullable.** Admin accounts should probably have a
password plus MFA rather than a single OTP channel.

---

## D27 — `requested_ip` on auth codes

**Three uses**, all real:
1. **Rate limiting** — "20 requests from one IP across 15 emails in 5 minutes" is
   enumeration.
2. **Takeover forensics** — requested in Lagos, consumed in Amsterdam 30 seconds
   later.
3. **Referral fraud** — "15 accounts from one IP each qualifying a referral" is
   the abuse pattern you'll actually see once credits are tied to invites.

GDPR: personal data. Purge `auth_codes` older than 90 days.

---

## D28 — `auth_identities` stays its own table

**Asked: do we really need this?** Yes, and more so with OTP-primary auth:

1. **Multiple providers per user.** Legacy `Registration format` was a single
   option set, so a Google signup could never also link LinkedIn.
2. **Account linking on email collision.** Someone signs up with OTP, later
   clicks "Sign in with Google" using the same address. Insert against a unique
   constraint, not a column update.
3. **Unlinking.** A DELETE, not a five-field nullable update with no audit trail.

---

## D29 — Messaging built in-house

**Decision.** `conversations` + `conversation_participants` + `messages` +
`message_attachments`.

**Why not Stream/Sendbird:** ~$400+/mo for 13 conversations and 44 messages. The
use case is low-volume transactional 1:1 messaging around a booking — closer to
email threading than community chat. You need send, receive, unread count,
realtime delivery, and file sharing. **Supabase Realtime** gives live delivery by
subscribing to Postgres changes on `messages`: no extra service, no extra cost,
no data leaving the database.

**Naming corrected.** Legacy names were inverted — `messageThreads` held
individual messages. `messageStarters` → `conversations`, `messageThreads` →
`messages`.

**`receivedBy` disappears.** With a participants table, recipients are everyone
in the conversation who isn't the sender. Storing it per message is redundant and
breaks with three people.

**`message_type` + `payload` added now** even though only `text` is used. Two
columns, zero cost, makes slash commands (`/booking` → a tappable card) a
frontend project later rather than a data migration.

**If action cards are built, three rules:**
1. Card state is **derived** from the referenced `session_id`, never stored in
   payload. No dual writes, no drift.
2. Cards **expire**. A booking offer from three weeks ago shouldn't be tappable.
3. Every card action goes through the **same endpoint** as the normal UI flow.
   The moment there are two ways to create a booking, they diverge.

This is also why in-house is right: Stream supports custom message types, but
you'd round-trip your own booking state through their infrastructure and
reconcile it. Here, an action card is a foreign key.

---

## D30 — Awards self-reported this phase

**Decision.** Option A — don't verify, label clearly. `verification_status`
defaults to `unverified`; nothing renders a checkmark.

**Why:** every verified claim is manual admin work and the queue never empties.

**Columns exist now** so switching on verify-on-request later is a feature flag,
not a migration.

**The UI decision matters more than the schema.** Label at the field level
("Awards — self-reported"), not a footer disclaimer. A checkmark next to
"Chevening Scholar" reads as endorsement even with a tooltip saying otherwise —
a real liability question once money is involved.

---

## D31 — User-chosen deletion with a grace period

**Decision.** `account_deletion_requests` with `soft` | `anonymize` and a
scheduled execution date.

**Why the delay** (14–30 days):
1. **Regret** — a meaningful share log back in to cancel.
2. **In-flight obligations** — someone with a confirmed session tomorrow
   shouldn't vanish and leave the mentor with a ghost booking.
3. **Batch execution** — anonymization touches many tables; a scheduled job is
   easier to make correct and idempotent than a request handler.

**"Delete completely" means anonymize, not `DELETE FROM users`.** True DELETE
would corrupt every mentor's completion count and rating, and destroy financial
records you're required to keep once payments exist. Anonymization satisfies GDPR
erasure — the person is no longer identifiable. Full field list in
`01_identity.sql`.

**Surface it honestly:** "your account and personal information will be
permanently deleted; anonymised session records are retained for platform
integrity." Users accept that when stated plainly.

---

## D32 — Multi-tenancy deferred, not pre-built **[REVISED]**

**Original advice:** add `organization_id` now, it's cheap.

**Why that changed:** it's not free. `organization_id` on every table means every
query, index, and RLS policy carries it — an indefinite tax for a maybe.

**Retrofit when needed:** create `organizations`, insert one default row, add
nullable `organization_id`, backfill, set NOT NULL. A weekend on this schema.

**The only thing to avoid meanwhile:** globally-unique human-readable keys that
would need to become tenant-scoped — referral codes, session-type slugs. UUIDv7
PKs already sidestep most of this.

---

## D33 — Audit log separate from session events

**Decision.** Both. `session_events` is domain state queried constantly by
product features and needs a rigid schema. `audit_log` is the generic catch-all.
Don't collapse them.

Dot-namespaced actions (`mentor.approved`, `credit.adjusted`) allow prefix
filtering without a rigid enum.

**Append-only.** `REVOKE UPDATE, DELETE ON audit_log FROM app_role`. An audit log
you can edit is not an audit log.

**Plan retention now.** Monthly partitioning or cold-storage archival after
12–24 months is far easier to decide than to retrofit at 50M rows.

---

## D34 — PostHog flags out of domain tables

**Decision.** `trackedSessionPosthog` and `sessionTrackedPosthog` are dropped.
`outbox_events` handles dispatch.

**Why:** those were dispatch bookkeeping leaking into domain tables. The sessions
table should not record whether you told an analytics vendor about it. A consumer
reads `session_events` (already an append-only stream), emits, and tracks its own
cursor.

---

## D35 — Composio retained, exit path documented

**Decision.** Keep Composio this phase. `calendar_connections` stores **no
tokens** — Composio holds the credentials, we hold a reference.

**Legacy archaeology:** the `cal*` columns (`calDefaultScheduleId`, `calEventId`,
`calClientId`) are **Cal.com's** API vocabulary, not Google's. Google Calendar
has no "default schedule ID." Dead weight from an abandoned integration — not
migrated.

**Known constraint:** on Composio's self-serve plans credentials pass through
Composio's cloud — even with your own OAuth app, their backend callback URL is
what gets registered. Self-hosting is Enterprise-only. So users' calendar tokens
live in a third party's infrastructure. Accepted for this phase.

**Google verification, the part nobody explains:** `calendar.events` and
`calendar.readonly` are **sensitive** scopes → app and brand verification, **no
CASA security assessment**. Weeks, mostly paperwork. (Gmail/Drive read are
*restricted* → CASA, months, thousands of dollars. Not needed here.) The horror
stories come from the restricted path.

**What shortens it:** a real privacy policy on your verified domain that
explicitly names the Google data you access and why, and a demo video showing the
actual consent screen and what happens with the data. Most rejections are for
vague privacy policies.

**No platform bypasses this** if you want your own brand on the consent screen.

**Exit path:** Nango (open source, self-hostable, white-label auth) or Google
Calendar API directly. The schema means switching touches one table and one
service module.
