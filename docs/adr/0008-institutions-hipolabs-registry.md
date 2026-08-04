# 8. Institutions: the hipolabs registry, populated on demand

Date: 2026-08-04

## Status

Accepted

## Context

ADR 0007 deferred this as the first of three conflicts it declined to settle, and
named it *"Institutions: ROR ID vs hipolabs registry"*. That framing is now the
least accurate part of the question, because only one side of it is still
standing.

**The migration package already argued this out and changed its mind.** D15 is
marked **[REVISED]**: it opens *"Original suggestion: seed from ROR, thousands of
rows"* and records the pushback as correct — *"mirroring a catalogue you can query
live is pointless"*. Its DDL followed: `institutions` in `00_foundation.sql` has
**no `ror_id` column at all**, which ADR 0007 itself records in point 5. ROR
survives in exactly one place, as a permitted value in
`source CHECK (source IN ('hipolabs', 'manual', 'ror'))` — a provenance label, not
an identifier.

**The table is anchored to `countries`.** `country_code` is
`char(2) NOT NULL REFERENCES countries(code)`, which is what makes D15's *"country
derives once at write"* structural rather than a convention somebody has to
remember. It also means `institutions` cannot ship before `countries` — and
`countries` shipped in M0 under settled decision #21's standing exception, *"a
foundation phase may carry a lookup whose rows the next phase's foreign keys
require."* That exception was written for this foreign key.

**So the conflict is not package-versus-repository. It is one settled decision
standing alone against everything else**, which is a different problem and has a
different remedy. This record resolves it in the opposite direction from ADR
0009, where the package was overridden. Stating that plainly matters: two records
settling package-versus-tier-2 in opposite directions look arbitrary unless each
says why it went the way it did. The difference is that D26 asserted controls
nobody had built, while D15 describes a mechanism the legacy application is
already running.

**Four places currently assert the ROR answer**, and they are not equally
harmless:

| Where | What it says | Severity |
|---|---|---|
| Settled decision #17 | Institutions are identified by ROR ID, synced from ROR's data dump | Wrong rationale, cited rather than re-derived |
| The `ror_id` domain-vocabulary entry | *"The canonical reference for 'which university'"* | **Worse.** An instruction to create a column the target schema does not have |
| `README.md` stack table | *"Institution data / ROR / Synced from ROR's data dump"* | Wrong, and the most public of the four |
| `docs/adr/README.md` row 0008 | The reserved title, naming the conflict | Superseded by this record |

**Settled decision #17's reasoning has to be met, not stepped around.** Its
argument is not that ROR is a nicer identifier. It is: *"Storage is not the
constraint — the whole registry is trivial for Postgres — the dependency on
someone else's uptime was."* Choosing hipolabs appears to reintroduce precisely
the dependency #17 existed to remove, and hipolabs is a static GitHub JSON file
with no SLA. A record that overturns #17 without answering that has overturned it
by ignoring it.

**Three facts answer it.**

1. **The dependency is on writes, never on reads.** The package stores the rows
   that are actually referenced, so hipolabs is consulted only when a user selects
   an institution not already present. Displaying existing education history, and
   every query over it, touches Postgres alone. #17's fear — an outage taking
   historical data with it — is what "a table rather than a string" prevents, and
   D15 gives that as its own second reason.
2. **An unmatched institution is survivable by design.**
   `education_entries.school_name_raw` is `NOT NULL` and always kept, while
   `institution_id` is nullable. An entry that never matches still displays what
   the user typed and can be linked later.
3. **The legacy application already runs on hipolabs.**
   `02_profiles.sql` records that *"Hipolabs autocomplete is already in use in
   Bubble"*. This is not a new dependency being taken on during a migration; it is
   the one already in production, which the ROR proposal would have replaced.

**The scale argument is the other half.** D15 expects **~200–400 rows** drawn from
940 legacy education entries, against roughly 9,000 for a mirrored catalogue. The
rows that exist are the ones somebody chose.

**One weakness is real and belongs in the record rather than in a footnote.** The
package states that *"Hipolabs is incomplete for African institutions"* — which is
this platform's primary market, not an edge case, and nobody has measured how
large it is. Three mechanisms recover from it, and they are worth naming because
the gap reads worse than it is: `alt_names text[]` holds local aliases, the
trigram index on `name` makes fuzzy matching cheap, and `source='manual'` covers
what neither reaches. The last of those is the fallback, not the plan.

**This record blocks nothing.** ADR 0007's deferral table says institutions block
M0 because `institutions` is created in `00_foundation.sql`. ADR 0011 already
moved the table to M2, with the education tables that first reference it, and said
so explicitly: *"The gate did not move because the decision was made; it moved
because the table did."* What remains is a contradiction to close, not a phase to
unblock — and closing it is still worth doing, because a settled decision is cited
rather than re-derived.

## Decision

**Institutions are the hipolabs registry, populated on demand and stored once
referenced. No ROR identifier ships, and this record makes no deviation from the
package.**

1. **Autocomplete is served live from hipolabs; the catalogue is never mirrored.**
   A row is written when a user selects an institution, upserted on `domain`.
2. **The primary key is the surrogate `uuid`, and `domain` is the natural key for
   upsert and deduplication** — `UNIQUE`, and null only for `source='manual'`.
   Settled decision #22 keys ISO lookups on their natural key; that rule does not
   reach here, because it is scoped to keys that are externally governed and
   stable, and a nullable column cannot be a primary key regardless.
3. **No `ror_id` column, now or at the end of this phase.** Settled decision #17
   is superseded on the identifier and on the sync strategy.
4. **`'ror'` stays in the `source` CHECK constraint.** It records where a row came
   from; it is not an implementation we are claiming to have. Removing it would be
   a deviation from the package for no gain, and would foreclose the revisit below.
   The reasoning ADR 0009 used to drop `auth_codes` — schema asserting an
   implementation we do not have — does not apply to a provenance value that costs
   one word.
5. **`education_entries.school_name_raw` is always kept and `institution_id` stays
   nullable.** This is what makes an incomplete registry a display concern rather
   than a data-loss one, and it is load-bearing for the African-coverage gap above.
6. **ROR is a documented revisit, not a rejected idea.** It would be reopened by
   institution identity becoming a matching problem across systems — mergers and
   renames breaking joins, or an external partner exchanging institution records.
   Adding it later means backfilling every row collected in the meantime, which is
   the cost of this decision and is worth naming now rather than discovering then.

### Rejected alternatives

**Sync the ROR data dump locally — settled decision #17 as written.** It buys a
persistent identifier that survives renames and merges, and removes a runtime
dependency. Rejected because it solves a problem this product does not have: no
feature matches institutions across systems, and none is planned this phase. It
would seed roughly 9,000 rows to serve 200–400 real ones, and it would replace a
dependency the legacy application has already been running for years with a
different one, during a migration. The strongest version of this argument is the
uptime point, and it is answered above — the dependency is on writes, not reads.

**Carry a nullable `ror_id` "for later".** The most tempting option, and it is how
the schema would drift. A column nothing populates is indistinguishable from a
column somebody forgot to populate, and the first join written against it would be
silently wrong on every row. If ROR is ever needed the backfill is the same work
whether the column was added now or then — the column is not the expensive part.

**Store the institution as free text with no table.** Simplest possible. Rejected
for D15's reasons, which are concrete rather than aesthetic: country would have to
be re-derived from the API on every profile render instead of resolved once at
write; *"mentors who studied in the UK"* becomes a runtime API fan-out rather than
a join; and nothing would stop two spellings of one university. The legacy system
did this and produced 940 rows of free text.

**Mirror the whole hipolabs list.** Removes the runtime dependency without ROR's
seeding cost. Rejected because it takes on a staleness problem — a mirror is wrong
the moment the source updates and nothing says when — to avoid a dependency that
only applies to new institution lookups. It is the ROR proposal with a cheaper
catalogue and the same argument against it.

## Consequences

**Settled decision #17 is superseded in full**, not in part: both its identifier
and its sync strategy are replaced. The `ror_id` domain-vocabulary entry goes with
it — that one matters more than the decision row, because it reads as an
instruction to create the column rather than as a rationale for a choice.

**On acceptance**, and not before: decision #17 is rewritten to the hipolabs
registry, the `ror_id` vocabulary entry is replaced by `institutions.domain`, and
`README.md`'s stack table is corrected to match. They are deliberately *not*
changed while this record is `Proposed`. The settled-decisions table is described
as "choices already made" and is loaded at the start of every build, so a row
there asserting an unaccepted decision would direct work before the decision
exists — the same sequencing ADR 0009 states for decisions #7 and #12.

**ADR 0007's deferral table is now stale in two ways.** It says this conflict
blocks M0, which ADR 0011 already corrected, and it frames the conflict as
package-versus-repository, which this record shows it never quite was. Both are
left in place: 0007 is accepted and immutable, and what it recorded is what was
believed when it was written.

**The hipolabs dependency is accepted knowingly, and nothing monitors it.** If the
GitHub repository behind it disappears, institution autocomplete stops and new
education entries fall back to `source='manual'` with a null `domain` — degraded,
not broken, and invisible until somebody reports it. This is the same shape as the
Composio outage in `failure-modes.md`, where an integration was dead for an unknown
length of time because nothing was watching. Naming it here does not fix it.

**Two M2 prerequisites exist that the M0 chain does not provide**, and they are
recorded here so they are not discovered when the migration is being written. The
`pg_trgm` extension serves `idx_institutions_name_trgm` and the fuzzy matching
above; the M0 migration installs `pgcrypto` only. The `lookup_status` enum
(`'approved'`, `'pending_review'`, `'merged'`, `'rejected'`) types
`institutions.status`, and settled decision #21 deliberately defers enums to the
phase that first uses them, so it is correctly absent rather than missing. Both
belong in the migration that creates the table.

**The African-coverage gap becomes a data-quality workstream, not a schema
question.** If the match rate against the legacy 940 entries is poor, the recovery
is alias curation and fuzzy-match tuning before it is `source='manual'`, and
somebody has to do that work. The schema carries all three paths; nobody has sized
which one does the heavy lifting.

### Confirmation

- **Mechanical, once built:** no `ror_id` column appears in the `institutions`
  model or anywhere in the migration chain. Not checkable today — `institutions`
  ships in M2, so until then the chain creates nothing for it to be absent from.
- **Mechanical, once built:** `education_entries.school_name_raw` is `NOT NULL`
  and `institution_id` is nullable. These are what make an incomplete registry
  survivable, so they are the assertions worth holding, and they arrive with the
  education tables.
- **Mechanical, once built, and only if somebody writes it:** that the migration
  creating `institutions` also creates `pg_trgm` and `lookup_status`. The chain
  test asserts extensions, functions and table names against fixed literals — it
  has **no type assertion at all** and inherits nothing automatically. M2 must
  extend it. Stated this way because "the test will catch it" is what would
  otherwise be assumed, and it would not.
- **Not mechanical:** nothing detects hipolabs becoming unavailable. There is no
  health check, no alert, and no synthetic lookup. The failure is silent and
  partial by construction.
- **Not mechanical:** nobody has measured the hipolabs match rate against this
  platform's institutions. This record accepts a known coverage gap without having
  sized it, and that is its weakest point.

### Open questions

- **The real match rate**, answerable from the export with the query the package
  supplies: `SELECT school_name_raw, count(*) FROM staging.education GROUP BY 1
  ORDER BY 2 DESC`. 200–400 distinct across the 940 rows means a near-straight
  lookup; 600+ with obvious variants means a fuzzy pass at `similarity() > 0.85`.
  Bubble already used hipolabs autocomplete, so the names may be cleaner than a
  free-text field would suggest. To be run when M2 starts.
- **Whether institution email domains are load-bearing anywhere.** Settled
  decision #17 called hipolabs *"the source of email domains"*, which suggests a
  verification use — confirming a mentor's institutional address — that no ADR
  records and no schema column implements. If that feature is wanted, `domain`
  being null for `source='manual'` becomes a gap rather than a detail.
- **Who curates `status = 'pending_review'`.** The package indexes it by
  `usage_count DESC`, so the mechanism exists and the owner does not.
