# 2. Bubble export strategy

Date: 2026-08-01

## Status

Accepted

## Context

The legacy application is a Bubble app at `app.edufurther.org`. Its data has to
reach the new Postgres schema once, correctly, during a short read-only freeze
(see ADR 0003). Several facts shape how:

**The volume is trivial and the fidelity requirement is not.** Roughly 700
community members, 15 mentors, and 5,000 logged mentoring minutes. This is not a
throughput problem. It is small enough to verify closely — closely enough to
hand-check every mentor record — and building streaming or change-data-capture
machinery for it would cost more than the data is worth.

**This is a reshape, not a lift-and-shift.** The new tables are being redefined
around how the product should work, not around how Bubble happened to model it.
One Bubble Thing may become several tables, or several may collapse into one.

**Two export transports exist and the choice is not yet made.** Bubble offers a
Data API (`/api/1.1/obj/<thing>`, token-authenticated, cursor-paginated at 100
records) and an admin-side export from the editor's Data tab. They are not
equivalent in a way that matters: **the Data API is subject to privacy rules and
the admin export is not.** A privacy rule can omit fields from an API response
with no error and a 200 status, so an API-based export can be quietly incomplete.

Further Bubble-specific facts that constrain any importer:

- `_id` is a stable `<ms-timestamp>x<random>` string. It is unique, and its
  leading component doubles as a creation-time cross-check.
- File and image fields are URLs on Bubble's `appforest_uf` S3 bucket. They stay
  reachable only while the Bubble app is paid for.
- Option sets export as display text, not stable identifiers.
- Lists of Things export as arrays of `_id` values.
- Password hashes are not exportable at all.

## Decision

**The export transport is an implementation detail behind a port; the strategy
below holds either way.**

1. **Snapshot first, transform second.** Every run captures Bubble's raw output
   to an immutable local snapshot before any transformation. Importers read the
   snapshot, never Bubble. The freeze window is short and Bubble becomes
   unavailable after cutover; a transform bug found later must be re-runnable
   against the original bytes rather than requiring a live re-export.

2. **Snapshots never enter git.** `data/`, `exports/`, `migration/out/` and
   `*.csv` are ignored. They contain roughly 700 members' names and email
   addresses. `gitleaks` scans for credentials, not PII, and would not catch it.

3. **Field completeness is verified against the editor, per Thing, before the
   snapshot is trusted.** Whichever transport is used, the importer asserts an
   expected field set and fails on a missing key rather than writing a NULL. A
   200 response is not evidence of a complete record.

4. **Every migrated table carries `legacy_id`** — nullable, unique, indexed,
   holding the Bubble `_id`. It is the idempotency key for re-runs and the join
   key for reconciliation. Where one Thing splits across several tables, an
   explicit mapping table records the relationship; a column alone cannot.

5. **Files are downloaded, not linked.** Mentor avatars are fetched to our own
   storage during migration. An `appforest_uf` URL stored in our database becomes
   a 404 the day the Bubble subscription lapses.

6. **Option sets map to explicit enums and fail loudly.** An unrecognised display
   value raises. Nothing defaults on unknown — that is how thirty mentors acquire
   the wrong specialisation silently.

7. **Importers are idempotent and re-runnable**, keyed on `legacy_id`. Running
   twice produces the same database as running once.

8. **Reconciliation asserts business invariants, not row-count parity.** Because
   the shapes differ deliberately, counts prove nothing. The report checks that
   no session lacks a mentor or a mentee, no availability is orphaned, no paid
   session lacks a ledger entry, and every source record maps to something.

9. **Identity does not migrate.** Password hashes are unavailable, so accounts
   are matched on email and every user completes a magic-link or reset flow on
   first login.

## Consequences

The transport decision can be deferred without blocking schema work, and can
change later without touching the importers.

Snapshot-first costs disk and one extra step, and buys the ability to re-run the
entire transform offline — which is what makes the rehearsals in ADR 0003
possible at all.

Verifying field completeness is manual per Thing and cannot be fully automated,
because the authority for "what fields exist" is the Bubble editor UI. This is
recorded as a known blind spot rather than papered over.

Deferring the transport means the privacy-rule risk stays live until it is
chosen. If the Data API is selected, field-count verification stops being a
precaution and becomes mandatory.
