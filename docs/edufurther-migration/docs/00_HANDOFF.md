# EduFurther — Bubble to Full-Stack Migration Handoff

**Status:** Schema settled, ready to build
**Scope:** Backend (BE) data model. Frontend is a separate project.
**Source:** Legacy Bubble app, 16 tables, ~6,700 rows

---

## Read this in order

| Doc | What it's for |
|---|---|
| `00_HANDOFF.md` | This file. Architecture, conventions, phases, what not to build. |
| `01_DECISIONS.md` | Every significant decision with its reasoning. Read before changing anything. |
| `02_FIELD_MAPPING.md` | Legacy field → new field, every column in the dump accounted for. |
| `03_MIGRATION_RUNBOOK.md` | ETL procedure, reconciliation, cutover. |
| `04_DEFERRED.md` | Things deliberately not built, and what triggers building them. |
| `../schema/*.sql` | The DDL. Numbered in dependency order. |

---

## The problem being solved

The legacy Bubble data model has three structural issues, all of which trace to
platform limitations rather than bad judgement:

1. **`User` was doing five jobs** — identity, profile, OAuth credentials,
   billing, and onboarding state in one table with ~30 columns.
2. **Denormalised data with no sync mechanism** — `Mentor (front search)` was a
   hand-maintained read model that drifted from its sources.
3. **Display formatting stored as data** — `12hr-localEndTime-TXT`,
   `24hr-localEndTime-TXT`, and `endTime` are three representations of one fact,
   which breaks across DST transitions.

Bubble made the correct thing expensive, so workarounds accumulated. Postgres
makes the correct thing cheap.

---

## What the new schema looks like

**66 tables** replacing 16. That number sounds alarming; it isn't. The count goes
up because each table now does exactly one thing, and each fact has exactly one
home. Roughly a third of the new tables are lookups and junctions that were
previously "lists of text" crammed into parent rows.

```
schema/
  00_foundation.sql        extensions, uuidv7, enums, lookups        14 tables
  01_identity.sql          users + 8 tables split out of `User`      10 tables
  02_profiles.sql          mentor/mentee profiles, education          8 tables
  03_availability.sql      availability rules, calendar OAuth         3 tables
  04_sessions.sql          the core domain, bookings merged          10 tables
  05_credits_reviews.sql   credit lots + ledger, referrals            5 tables
  06_policy_standing.sql   booking limits, penalties, reports         6 tables
  07_communications.sql    notifications, delivery, messaging         9 tables
  08_features_platform.sql vision boards, audit, platform            10 tables
  09_reporting.sql         curated views for Metabase                 7 views
```

Run them in numeric order. Each is a single transaction and re-runnable against
an empty database.

### Validation status

All 246 statements parse against the Postgres grammar. No forward references, no
duplicate tables, and all 66 tables carry `created_at` + `updated_at`.

---

## Five things worth understanding before you touch the schema

### 1. Authorization comes from profile existence, not a `role` column

`users.primary_role` is a **UX hint only** — which dashboard someone lands on.
It must never gate a permission.

```sql
-- can be booked
EXISTS (SELECT 1 FROM mentor_profiles
        WHERE user_id = ? AND approval_status = 'approved')

-- can book
EXISTS (SELECT 1 FROM mentee_goals WHERE user_id = ?)
```

This is what makes dual roles (a mentor who is also a mentee) work from day one
at zero cost. A `role` column that gates permissions has to stay consistent with
the profile tables and can silently disagree with them; profile existence cannot.

**Never write `WHERE primary_role = 'mentor'` in an authorization check.**

### 2. `user_id` is the primary key of every 1:1 extension

`user_profiles`, `mentor_profiles`, `user_onboarding`, `mentee_goals`,
`user_standing` — all keyed on `user_id`, no surrogate `id`.

This removes the `mentor_profile_id` vs `user_id` ambiguity entirely: they're the
same value. It also makes the 1:1 relationship structurally enforced rather than
conventional.

### 3. Derived data is never stored

Session counts, review counts, completion percentages, availability status,
unread counts — all computed at query time. At this data volume they're free, and
storing them recreates exactly the drift that made `Mentor (front search)`
untrustworthy.

The exception is `conversations.last_message_preview`, which is maintained by a
**trigger**, not application code.

### 4. Soft delete everywhere, with the partial unique index

```sql
deleted_at timestamptz
CREATE UNIQUE INDEX ON users (email) WHERE deleted_at IS NULL;
```

Without that `WHERE` clause, a soft-deleted user permanently blocks their own
email from re-registering. Two boundaries:

- **Soft delete is not a status.** A cancelled session is `status = 'cancelled'`,
  still visible and counted. Don't soft-delete it.
- **Never `ON DELETE CASCADE` in a soft-delete system.** Cascade is a hard-delete
  mechanism. Deletion propagation is application logic, explicit and tested.

### 5. Timestamps are enforced, not intended

Every table has `created_at` + `updated_at`, both `timestamptz`. The
`set_updated_at()` trigger is attached by `attach_updated_at_triggers()` at the
end of each migration file.

**Add this to CI** — it should return zero rows:

```sql
SELECT c.relname AS table_missing_timestamps
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
  AND (NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                   AND a.attname = 'created_at' AND NOT a.attisdropped)
    OR NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                   AND a.attname = 'updated_at' AND NOT a.attisdropped));
```

Always `timestamptz`, never `timestamp`. Postgres stores UTC either way, but
`timestamp` silently discards offset information on write — which will bite you
across Lagos, Toronto, and Berlin.

---

## Conventions

| Area | Rule |
|---|---|
| **Naming** | `snake_case`, plural tables, `id`, FKs as `mentor_id` not `Fk_mentor`. No spaces, slashes, or `(Y/N)` — `google/dailyRoomName` requires quoting in every query you'll ever write. |
| **IDs** | UUIDv7. Time-ordered (good index locality), globally unique, doesn't leak row counts. `uuid_generate_v7()` shim provided for PG < 18. |
| **Timestamps** | `timestamptz` always. `created_at` + `updated_at` on every table. |
| **`created_by`** | On anything with a meaningful actor. Nullable — a null actor meaning "the system" is more honest than a fake system user ID. **Distinct from the domain owner**: `sessions.mentee_id` is who it's *for*, `created_by` is who *clicked book*. They diverge when a mentor or admin books on someone's behalf. |
| **Enums vs lookups** | If adding a value requires a code change anyway → enum. If it doesn't → lookup table. If it's a standard → use the standard (ISO 3166-1 countries, ISO 639-3 languages). |
| **Lists** | Every legacy "list of X" becomes a junction table. |
| **Migration anchor** | `legacy_bubble_id text UNIQUE` on every migrated table. Non-negotiable — makes the ETL re-runnable and reconciliation possible. |

---

## Migration phases

Two tracks. They're different kinds of work and can partly run in parallel.

### Migration track — has Bubble source data

Each phase: extract via Bubble Data API → `staging` schema as raw jsonb →
transform → load → reconcile. **Never transform on read** — when a transform is
wrong you re-run from staging, not from Bubble's rate-limited API.

| Phase | Tables | Rows | Risk |
|---|---|---|---|
| **M0** Foundation | extensions, enums, `languages` + `countries` seeds, triggers | — | Low |
| **M1** Identity | users, profiles, auth_identities, onboarding, admin_users, user_languages, calendar_connections | ~2,100 | **Critical** |
| **M2** Attributes | education_entries, institutions, mentee_goals, mentor_profiles, user_awards, scholarship experience, service offerings | ~1,750 | Medium |
| **M3** Availability | availability_rules, availability_exceptions | ~200 | **High** — timezone/DST |
| **M4** Sessions | sessions, participants, events, auto-created session types | ~2,000 | **High** — booking/tracker merge |
| **M5** Derived | reviews, opening credit lots | ~70 | Low |
| **M6** Communications | notifications, recipients, conversations, messages | ~750 | Low |

**Reconcile M1 completely before starting M2.** Everything downstream depends on
it. Row counts, null rates on required fields, field-level diffs on 20 random
records per table.

### Build track — greenfield, no source data

| Phase | Tables | Depends on |
|---|---|---|
| **B1** Credits & referrals | credit_lots, credit_transactions, referrals, referral_unlocks | M1 |
| **B2** Policy & standing | booking_policies, booking_attempts, infractions, standing, blocks, reports | M4 |
| **B3** Notification delivery | outbox, push_subscriptions, whatsapp_templates, channel_preferences | M6 |
| **B4** Session types & intake | session_types, configs, questions, options, submissions, answers | M2 |
| **B5** Vision boards | vision_boards, milestones — redesign, then hand-migrate 10 rows | M1 |
| **B6** Reporting | `reporting` schema views for Metabase | M4 |

### Cross-cutting — start immediately

**File migration off Bubble's S3.** `User Profile image`, `Profile banner Image`,
`pictureProfile`, `visionBoardCardShareImg`, `visionBoardCertShareImg` all
currently point at Bubble-hosted URLs — a live dependency on the platform you're
leaving. Download every file, re-upload to your own storage, rewrite URLs during
ETL. Network-bound, slow, schema-independent, and easy to forget until cutover.

**Google OAuth verification.** Calendar scopes are *sensitive* tier — app and
brand verification, no CASA security assessment. Weeks, mostly paperwork. It's
calendar-time not eng-time, and it's the long pole. Start now.

**Meta Business verification** for WhatsApp. Days to weeks.

---

## What NOT to build this phase

Stated explicitly, because each of these was considered and rejected for a
reason:

| Skip | Why |
|---|---|
| **Redis / caching layer** | Indexes + TanStack Query cover it at 1,200 users. Adding Redis now buys a cache-invalidation bug, not speed. |
| **Typesense / Meilisearch** | Postgres FTS + `pg_trgm` is enough until ~10,000 mentors. At 44, a 6-way join is sub-millisecond. |
| **Third-party chat** (Stream, Sendbird) | ~$400+/mo for 13 conversations. Supabase Realtime is free and already available. |
| **Novu** | 681 notifications doesn't justify it. Two tables + a dispatcher + web-push + the existing email sender. |
| **Multi-tenancy** (`organization_id`) | Retrofit is a weekend on this schema. Just avoid globally-unique human-readable keys (referral codes, session-type slugs) that would need to become tenant-scoped. |
| **Capacitor** | Ship the PWA, measure iOS home-screen install rate. If notifications matter and that number is bad, Capacitor reuses the React code — weeks, not a rewrite. Don't use Capacitor-specific storage APIs so the port stays clean. |
| **pgvector matching** | 44 mentors is too small a corpus for embeddings to beat good filters + rating sort. |
| **`tags` taxonomy** | Deferred. `service_offerings` is the seed; see `04_DEFERRED.md`. |

### Do build now

- **Cursor pagination on every list endpoint.** Retrofitting is a breaking API change.
- **`pg_stat_statements`** and a 100ms slow-query threshold. You want the data before you need it.
- **The `reporting` schema.** Makes dashboards survive refactors, and makes Metabase's natural-language querying actually work.
- **Idempotency keys on booking.** Mandatory before payments; cheap now.
- **`message_type` + `payload` on messages.** Two columns, zero cost, makes slash commands a frontend project later rather than a data migration.

---

## Frontend performance approach

Layer in this order, stop as soon as it's fast enough:

1. **Database** — the indexes in the DDL. Solves essentially everything at this volume.
2. **HTTP cache headers** — `Cache-Control` + `ETag` on genuinely public data.
3. **Application cache (Redis)** — only where measured. Never cache credit balances, session status, or unread counts: anything where staleness produces a *wrong action*.
4. **TanStack Query** — `staleTime` by data volatility:

| Data | staleTime |
|---|---|
| Mentor list, tags, languages | 5 min |
| Mentor profile detail | 2 min |
| Availability slots | 30 s |
| Session list | 30 s |
| Credit balance, unread counts | 0 |

5. **Optimistic updates** on send-message and mark-read.

Revisit when p95 on a real endpoint crosses ~300ms under actual traffic.

---

## Internal analytics

**Never point a BI tool at the production primary.** One unindexed dashboard query
scanning sessions will degrade live bookings.

- **Read replica** — a config toggle on managed Postgres.
- **Metabase OSS**, self-hosted via Docker (~$12–20/mo VPS). AGPL v3 only binds
  if you modify it and offer it as a service; internal unmodified use is
  unrestricted.
- **Metabase AI is now in the open-source edition** (v60, April 2026) with
  bring-your-own-model — plug in an Anthropic key, pay per token. It follows
  existing permission rules and links every answer to the underlying query.
- **The `reporting` schema matters more than the tool.** Turn AI on after the
  views exist; it amplifies a good semantic layer and flails without one.

Sequencing: ship the views in B6, point Supabase's SQL editor at them, add
Metabase when a non-SQL team member first asks for a number.

---

## Open decisions

Two remain. Neither blocks starting M0/M1.

| # | Decision | Recommendation | Blocks |
|---|---|---|---|
| 1 | What counts as a **qualifying invite** | Invitee completes onboarding. Email-verified alone is farmable with disposable addresses; session-completed delays the referrer's reward by days and weakens the loop. | B1 |
| 2 | **Payments architecture** | Separate discussion. The lot model keeps it additive — `unit_cost_cents` and `currency` sit at zero until needed. | Future |

Previously open, now resolved: dual roles (profile-existence authorization makes
it free), verification policy (self-reported this phase), institutions
(lazily-populated registry), scholarship modelling (user-level).

---

## Product observations worth acting on

Things the data surfaced that aren't schema problems:

- **5.7% review rate** — 53 reviews against 935 sessions. `v_review_coverage`
  tracks it from day one.
- **10 vision boards** — suggests a discovery or onboarding problem, not a
  concept problem. Badge-driven milestone completion is a real retention
  mechanic. Run a null-count on those 10 rows before the redesign session; half
  those columns are probably empty in all 10.
- **No phone numbers anywhere** — WhatsApp reaches nobody whose number you don't
  have. Existing 1,200 users need a collection campaign.
- **44 mentors, 720 mentee goals** — the constraint is mentor supply, not demand.
  Worth remembering when tuning booking concurrency limits.
