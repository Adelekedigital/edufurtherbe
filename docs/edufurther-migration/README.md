# EduFurther — Bubble Migration Handoff

Backend data model for migrating EduFurther off Bubble into a self-hosted
Postgres stack.

## Contents

```
docs/
  00_HANDOFF.md            START HERE. Architecture, conventions, phases.
  01_DECISIONS.md          35 decisions with reasoning. Read before changing anything.
  02_FIELD_MAPPING.md      Every legacy field -> its new home.
  03_MIGRATION_RUNBOOK.md  ETL procedure, reconciliation queries, cutover plan.
  04_DEFERRED.md           What wasn't built, why, and the trigger to build it.

schema/
  00_foundation.sql        extensions, uuidv7, 40 enums, 7 lookup tables
  01_identity.sql          users + 9 tables split out of legacy `User`
  02_profiles.sql          mentor/mentee profiles, education, credentials
  03_availability.sql      availability rules, exceptions, calendar OAuth
  04_sessions.sql          merged bookings/trackers, events, session types, intake
  05_credits_reviews.sql   credit lots + ledger, referrals, reviews
  06_policy_standing.sql   booking limits, penalties, reports
  07_communications.sql    notifications, delivery channels, messaging
  08_features_platform.sql vision boards, audit, idempotency, feature flags
  09_reporting.sql         curated views for Metabase
```

## Running the schema

```bash
for f in schema/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

Numeric order matters — files depend on earlier ones. Each is a single
transaction and re-runnable against an empty database.

Requires Postgres 14+. On PG 18+, replace `uuid_generate_v7()` with the native
`uuidv7()` and drop the shim in `00_foundation.sql`.

## Validation status

- 246 statements, all parse against the Postgres grammar
- 66 tables, 40 enums, no forward references, no duplicates
- Every table carries `created_at` + `updated_at` (CI query in `00_HANDOFF.md`)

## Scale reference

| Legacy | Rows |
|---|---|
| User | 1,200 |
| SessionBooking | 1,073 |
| SessionTracker | 935 |
| Education | 940 |
| PersonalInfo | 858 |
| Members Goals | 720 |
| Notifications | 681 |
| CalendarSettings | 192 |
| Reviews | 53 |
| Mentor (front search) | 44 |
| messageThreads | 44 |
| Mentor Services | 31 |
| Scholarship-Awards | 17 |
| messageStarters | 13 |
| VB-Vision Boards | 10 |
| CalendarExtra | 5 |

Total ~6,800 rows. Small enough that most performance concerns are premature —
which is why `04_DEFERRED.md` is as long as it is.

## Open decisions

1. **Qualifying invite definition** — recommend invitee-completes-onboarding. Blocks B1.
2. **Payments architecture** — separate discussion. The lot model keeps it additive.
