# 20. Mirror the institution catalogue, refreshed weekly

Date: 2026-08-08

## Status

Accepted.

The GitHub Actions scheduling mechanism is superseded by ADR 0028. The source,
mirror, weekly cadence, and idempotency decisions remain.

Supersedes ADR 0008 **on the storage strategy and the autocomplete mechanism
only**. The rest of 0008 stands untouched: hipolabs remains the source, the
surrogate `uuid` remains the primary key, `domain` remains the natural key, and
there is still no `ror_id`.

## Context

ADR 0008 decided that institutions are *"populated on demand and stored once
referenced"*, that *"autocomplete is served live from hipolabs"*, and it
explicitly rejected **"Mirror the whole hipolabs list"** — because *"it takes on
a staleness problem: a mirror is wrong the moment the source updates and nothing
says when."*

That rejection was sound on its own terms. Two things have changed since, and one
of them means 0008's mechanism cannot be built at all.

**1. hipolabs serves plain HTTP, with no TLS.** Its API base is
`http://universities.hipolabs.com`, confirmed from the project's own
documentation. A browser on an HTTPS page **cannot** call an HTTP endpoint — it
is blocked as mixed content, universally, with no opt-out. So *"user types →
hipolabs autocomplete (client-side, no storage)"* is not implementable. The only
live alternative is proxying the unencrypted endpoint through our own backend on
the request path, which is worse than what 0008 described rather than better.

**2. Live autocomplete had already been deferred.** M2 chose to serve
`GET /institutions?q=` from our own table and defer the live path. Nobody
revisited 0008's rejection afterwards, which had assumed the live path was the
fallback for anything not yet stored. The result was a state nobody chose: a
table of a few hundred rows acting as the entire selectable universe, with
nothing behind it.

**Five things are now measured rather than assumed.**

| | |
|---|---|
| Catalogue size | **10,257 records, 2.25 MB** |
| Available over HTTPS | **yes** — at `raw.githubusercontent.com`, even though the API is not |
| Change cadence | **one change every ~2 days** — 100 commits over 177 days |
| Match rate against the legacy extract | **18 of 18** distinct school names, exact — including **5 of 5** African institutions |
| Country codes we cannot resolve | **1** (`XK`, Kosovo — a user-assigned code, not official ISO 3166-1), covering 5 records |

The match rate answers the question 0008 named as its own weakest point:
*"nobody has measured the hipolabs match rate against this platform's
institutions."* The mechanism explains the result — the legacy application
populated those names *from* hipolabs autocomplete, so they are hipolabs' own
strings.

The cadence measurement cuts the other way, and is recorded because it makes
0008's objection **stronger** than it was credited: a mirror really does go stale
quickly, so a refresh is part of this decision rather than an improvement on it.

## Decision

**The catalogue is mirrored into `institutions`, fetched over HTTPS from the
source repository. The HTTP API is never called — not at request time, not in
the ETL, not from a browser.**

1. **The source is the published JSON file over HTTPS.** The `http` API is not a
   fallback; it is not called at all. The asymmetry that makes this clean is that
   the *data* is available securely even though the *API* is not.
2. **Refreshed weekly** by `scripts/sync_institutions.py`. **One implementation,
   two triggers** — a scheduled GitHub Actions workflow, and the same script run
   by hand. A separate scheduled path would drift, and the one nobody invokes by
   hand is the one that rots unnoticed.
3. **GitHub Actions, not a queue vendor.** `migrate.yml` already reaches the
   database from a runner, so the mechanism and the credential exist. Settled
   decision #13 keeps the FastAPI Cloud → Railway exit real by using no
   *platform-native* cron; Actions moves with the repository rather than the
   host, so the exit is untouched and no vendor is added to hold a schedule.
4. **`institutions.last_synced_at` records when a row was last seen upstream.**
   Every row the sync saw is stamped, including unchanged ones. This is the
   direct answer to 0008's objection: `max(last_synced_at)` says how stale the
   mirror is, and a row behind that maximum is one the source no longer carries.
   `updated_at` keeps its own meaning — it moves only when a row's content
   actually changed.
5. **Matching is exact, or case-folded, or nothing — and a name the catalogue
   carries twice is none of them.** Upstream holds **73 exactly-duplicated names
   over 158 records**, and they cross borders: `City University` is a university
   in the United States, in Bangladesh *and* in the United Kingdom. A plain
   lookup keeps whichever row a `SELECT` without `ORDER BY` returned last, so
   the link would be a coin toss that changes between runs — and the country of
   study derives from the winner. An ambiguous name is reported as a question,
   separately from a miss, because the two need different work: a miss wants the
   institution added, an ambiguity wants somebody to say *which*. No fuzzy tier
   either. Measured
   against real names, a genuine typo scores **0.773** while
   `Federal University of Technology, Yola` and `… Akure` — two different
   Nigerian universities — score **0.750**. No threshold separates them, and the
   two failure modes are not symmetric: a wrong link is silent and permanent, and
   the study country derives from it, while a miss is visible and recoverable
   because `education_entries.school_name_raw` is always kept.
6. **`institutions.country_id` becomes nullable.** A mirrored row always carries
   a country; a user-created one is completed by an admin during the
   `pending_review` pass, rather than the user being asked for a field the review
   process exists to supply.
7. **A record with no domain is refused, and an unresolvable country is skipped
   and reported by code.** Neither is defaulted. A wrong country propagates into
   *"who studied in the UK"* and nothing would ever surface it.

### Rejected alternatives

**Populate on demand with live autocomplete — ADR 0008 as written.** Rejected
because it cannot be built: the endpoint is HTTP-only and a browser will not call
it from an HTTPS page. Proxying it server-side replaces a client-side dependency
with a request-path dependency on an unencrypted third party that nothing
monitors.

**Populate on demand with no live path — the state M2 was actually in.**
Rejected because a few hundred rows then constitute the entire selectable
universe. Users are not blocked — a free-typed name creates a `source='manual'`
row — but every unlisted school becomes an entry in a curation queue that 0008
itself says has no owner, and at 1,200 users that mints the duplicate problem
rather than avoiding it.

**Mirror with no refresh.** Rejected on the measurement: the source changes every
~2 days, so a one-off snapshot is wrong within a fortnight and nothing would say
so. This is 0008's objection, and it is correct.

**Daily or hourly refresh.** Rejected as disproportionate. Changes are
overwhelmingly additions of institutions nobody has asked for yet, so weekly
bounds staleness at ~7 days for a fraction of the runs.

**A queue vendor (QStash or similar) to hold the schedule.** Rejected because the
schedule can be held for free by infrastructure already in use, and a vendor
adopted to run one weekly job is a credential, an outage surface and an ADR that
buys nothing. It would earn its own record if the refresh ever needs to run from
inside the application.

## Consequences

**ADR 0008 is superseded in part, not in whole**, and its status names the part.
Its reasoning about *why a table rather than a string* — country derived once at
write, `school_name_raw` always kept, an unmatched entry degrading display rather
than losing data — is untouched, and is exactly what makes this safe.

**A fresh environment has an empty catalogue until the sync runs.** That is a
deployment step. It is stated here because it will otherwise be discovered by
whoever provisions the next environment and finds autocomplete empty.

**Two upstream records share one domain** (`khio.no`, `jazanu.edu.sa` — a merged
art school and a college inside a university). They are collapsed to one row
**before the write**, keeping the first, and the collapse is counted so the row
count is not read as a loss. Letting `ON CONFLICT` absorb the pair instead writes
both, so the second rewrites the first on every sync forever while the stored
content is identical every time — and `updated_at` would come to mean "a sync
ran", which is exactly what `last_synced_at` exists to say. A conditional upsert
does not help: the two records genuinely differ, so the write is never a no-op.

**Staleness is now bounded and answerable rather than unbounded and invisible —
but it is not zero, and nothing alerts on a sync that has stopped.**
`max(last_synced_at)` makes it a query; nobody is yet assigned to run that query.
This is the same shape as the `pending_review` queue 0008 flagged, and naming it
again does not fix it.

**`country_id` nullable weakens a real invariant.** A manual institution can
exist without a country, and until an admin completes it that user's education
entry contributes nothing to *"who studied in the UK"*. Accepted deliberately:
the alternative asks a user to do the curation the review process exists for.

### Confirmation

- **Mechanical:** no request to `universities.hipolabs.com` appears anywhere in
  the codebase.
- **Mechanical:** a second sync writes no new rows and moves no `updated_at`,
  while `last_synced_at` does move — asserted for a domain that appears once
  *and* for one that appears twice. The second case was broken and the first
  test could not see it.
- **Mechanical:** a near-miss does not link — proved by reintroducing a prefix
  fallback and watching the test fail.
- **Mechanical:** an unresolvable country code is skipped and named, never
  defaulted.
- **Mechanical:** a name the catalogue carries twice does not link, and is
  reported apart from a miss — proved by mutation, including the variant that
  folds it back into the miss count.
- **Mechanical:** a catalogue whose records are all refused exits non-zero
  rather than mirroring nothing and reporting success. An upstream key rename
  is the realistic trigger, and the dry run CI runs first must fail on it too.
- **Not mechanical:** nobody watches `last_synced_at`. A workflow that silently
  stops firing is invisible until somebody looks.
- **Not mechanical:** the 18/18 match rate is measured on 18 names. The
  production import against 940 is where it is really tested, and a materially
  worse rate would reopen the fuzzy-matching question this record closes.
