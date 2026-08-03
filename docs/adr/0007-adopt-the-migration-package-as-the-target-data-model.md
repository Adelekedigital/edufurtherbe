# 7. Adopt the migration package as the target data model

Date: 2026-08-03

## Status

Accepted

## Context

A migration package was produced for the Bubble reshape and sits at
`docs/edufurther-migration/`: 66 tables and 40 enums — both counted from the DDL
rather than taken on trust — across ten numbered files, plus a decisions log, a
field mapping covering every legacy column, and a runbook. The package also
reports 246 DDL statements; that figure is its own and is **not** independently
verified here. It is the first artefact that answers "what does the new schema
actually look like" rather than describing how to arrive at one.

**It was received, not authored here**, and it was written against a snapshot of
this repository that predates ADRs 0004, 0005 and 0006. That timing is the whole
problem this record exists to solve. Seven points where the package and this
repository disagree were found by reading both:

1. Column naming — `legacy_id` here, `legacy_bubble_id` in the schema.
2. Whether a booking and a session are one row or two.
3. The Bubble export transport, which ADR 0002 deliberately deferred.
4. `conversation_id`, which the domain vocabulary already reserves for a vendor
   identifier and the package uses for its own messaging foreign key.
5. Institutions — settled decision #17 identifies them by ROR ID; the package
   rejects seeding ROR and keys on the hipolabs domain instead, and its DDL has
   no `ror_id` column at all.
6. First-login authentication — decision #7 and ADR 0002 say magic link; the
   package chooses a 6-digit OTP, and that reasoning reaches into ADR 0005, which
   partly justified Supabase on magic link arriving unbuilt.
7. Message thread scope — ADR 0006 decides a thread belongs to a booking and
   makes that its confirmation criterion; the package's `conversations` table has
   no session foreign key and threads stand alone.

The first four are documentation lagging behind a decision that has already been
argued and settled. The last three are genuine design decisions with a column or
a foreign key on the other end, and they are **not** settled by this record.

The question decided here is narrow: what is the canonical description of the
target schema, and which repository documents does it correct.

## Decision

**`docs/edufurther-migration/` is the canonical target data model.** It is a
received artefact. It is committed to this repository so the reference resolves,
and it is **not edited here**. Where it and a repository document disagree, this
record names the winner, point by point. Where a later decision supersedes part
of it, that is a new ADR and the package text stays as it was received.

Four points are settled:

1. **`legacy_bubble_id`** is the migration anchor column on every migrated table.
   This supersedes ADR 0002 point 4, which named it `legacy_id`. The name is the
   only thing that changes; nullable, unique, indexed, and idempotency keyed on
   it all stand.

2. **A booking and a session are one row.** `sessions` carries a lifecycle
   status; "a booking" is the act of claiming a slot, and "it happened" is
   `status = 'completed'`. The legacy split across `SessionBooking` and
   `SessionTracker` was a platform workaround — both tables carried the same
   meeting venue and room columns — not a domain distinction. The domain
   vocabulary is corrected accordingly. ADR 0006 is unaffected: a thread that
   belongs to a booking belongs to the row that booking created.

3. **The export transport is the Bubble Data API**, staged as raw `jsonb` in a
   `staging` schema before any transform. This **resolves** the deferral ADR 0002
   left open rather than superseding it, and it activates that record's own
   clause: per-Thing field-set verification is now mandatory rather than
   precautionary, because the Data API is subject to privacy rules that can omit
   a field with a 200 response and no error.

4. **`whatsapp_conversation_id`** is the vendor thread identifier in the domain
   vocabulary. `conversation_id` belongs to our own messaging tables. A vendor
   identifier gets the vendor's name on it; the collision is resolved against the
   vendor because the package's `conversations` table is ours and idiomatic, and
   renaming it would put the DDL, the field mapping and the notifications foreign
   key out of step to protect someone else's naming.

**What this record does not settle**, listed so that a reader does not mistake
reconciliation for completeness:

| Conflict | Blocks | Record |
|---|---|---|
| Institutions: ROR ID vs hipolabs registry | M0 — `institutions` is created in `00_foundation.sql` | ADR 0008 |
| First-login authentication: magic link vs OTP | M1 — the critical phase | ADR 0009 |
| Message thread scope: booking-scoped vs standalone | M6 | ADR 0010 |

Until those land, the repository is knowingly inconsistent on three points. That
is preferable to recording a decision nobody made.

### Rejected alternatives

**Rewrite the package's content into repository documents.** Would produce one
voice and one place to look. Rejected because it discards provenance — the
package's value includes being a dated artefact from outside this repository,
with its own record of what was revised during design and why — and because the
DDL is the thing that runs. A transcribed copy of a schema is a second copy that
drifts from the one you execute.

**Treat the package as advisory input only.** Cheapest, and it avoids committing
to a schema before the first migration is written. Rejected because it leaves two
answers to every schema question and no rule for choosing, which is the condition
this record exists to end. The strongest argument for it is that the package has
not yet been executed against a real database, so adopting it is a commitment
made on reading rather than on running.

**Settle all seven conflicts in one record.** Tempting, and it would look
complete. Rejected because three of them are decisions rather than
reconciliations, and a record that appears to resolve everything while three
conflicts remain open is worse than one that names them — the next person builds
on it believing the question is closed.

## Consequences

There is one answer per schema question **on the four points settled here**. M0
and M1 are not thereby unblocked: `institutions` is created in M0 and waits on
ADR 0008, identity is M1 and waits on ADR 0009. What this record removes is
ambiguity, not those two gates.

The package is now in git, so `docs/edufurther-migration/` resolves for anyone
cloning the repository, and the "Where the truth lives" pointer is not dangling.
The loose duplicate summary at `docs/data-model-migration.html` is deleted; it
was byte-identical to `SUMMARY.html` apart from line endings, and two copies of
one document is precisely what the canonical copy rule prohibits.

**The staging schema holds 1,200 users' personal data, inside the Supabase
project chosen by ADR 0005.** The PII guardrail was written when snapshots were
expected to be files, and its `data/`, `exports/`, `*.csv` wording no longer
describes where the data actually lands. The guardrail is widened; the underlying
exposure is new and is a consequence of this decision, not of the guardrail.

**The raw extract will contain live OAuth credentials.** `calAccessToken` and
`calRefreshToken` sit on the legacy `User` table. The package correctly does not
migrate them, but "extract everything, then transform" means they land in staging
first and sit there through every rehearsal. Redaction belongs at extraction,
before the insert, and **nothing does it today.**

**Field completeness is an open obligation, not a solved problem.** Choosing the
Data API activates ADR 0002's clause, and the runbook notes the privacy-rule risk
in prose without a failing assertion. A silently missing column is worse than an
error, and at present nothing turns one into the other.

**The package duplicates legacy field lists that `docs/bubble-data-model.md`
holds canonically.** `02_FIELD_MAPPING.md` restates the legacy shape alongside
its mapping. Since the package is not edited here, the two copies cannot be
reconciled by editing — the mitigation is that `docs/bubble-data-model.md`
remains canonical for what the legacy system contains, and the package is
canonical only for what the target contains.

**Adopting a schema that has never been executed is a commitment on paper.** The
246 statements are reported as parsing against the Postgres grammar; parsing is
not running, and nothing here has been applied to a database.

**The package's self-reported figures are not all correct, and we inherit them.**
`00_HANDOFF.md` says `09_reporting.sql` contains 7 views; it contains 8. The
table and enum counts check out exactly, which is what makes the view count worth
recording — the error is isolated rather than systematic, and since the package
is not edited here it cannot be corrected in place. Treat its prose counts as
indicative and the DDL as authoritative.

### Confirmation

- **Mechanical:** `docs/edufurther-migration/` is tracked, so the reference from
  `project-conventions` resolves. A dangling pointer would show as a broken link.
- **Mechanical:** `legacy_id` appears nowhere outside ADR 0002's immutable text
  and this record's account of the rename. Written the obvious way — "appears
  nowhere outside ADR 0002" — the criterion is falsified by the sentence above
  that states the rename, which is worth noticing: a confirmation that its own
  record breaks was never a check.
- **Not mechanical:** nothing prevents the package being edited in place, which
  would destroy both its provenance and the rule that makes it trustworthy.
  Enforced by review against this record.
- **Not mechanical, and a live gap:** nothing asserts per-Thing field
  completeness at extraction, and nothing redacts credential fields before they
  reach staging. Both are named above as obligations and both are unbuilt. This
  is the weakest part of this record.
- **Not mechanical:** nothing checks that the package's DDL and the Alembic
  migrations written later stay in agreement. The package is a target, not a
  schema that runs in CI, and the divergence will be silent when it starts.

### Open questions

- The three conflicts deferred to ADRs 0008, 0009 and 0010.
- **Whether the package's DDL becomes the migration chain or a specification for
  it.** Ten files that run in order against an empty database is not the same
  artefact as an Alembic chain that must also run forward from a populated one.
  Decide before M0 is written.
- **Where credential redaction lives** — in the extractor, or as a staging-schema
  policy. It has to exist before the first rehearsal, not before cutover.
