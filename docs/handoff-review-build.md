# Handoff — the review build (M5a)

**Status:** PRs 1 and 2 are built — the table, and the write surface with the
rules that guard it. Four PRs and one ADR remain.
**Written:** 2026-08-20, from `FE-ui-guide/reviewUI/` measured against the codebase,
`docs/edufurther-migration/` and the dev Bubble app.
**Companion:** `handoff-session-build.md`, whose sequence this follows.

Read this before opening the UI files. It is the record of what was decided and
why, so the decisions do not get re-derived, re-argued, or silently reversed.

---

## The one-line state

**M5 splits in two. This is M5a — reviews and nothing else.** Credits become
**M5b**. It was scoped as "three tables and two loaders, no API" on the grounds
that no UI existed. The review UI landed on 2026-08-20 and that reasoning
lapsed — settled decision #21's condition is now met, so the write and read
surfaces are in scope and the phase roughly doubled.

**Why lettered rather than a new number.** `M1c` and `M2c` already exist in this
repository, so splitting a package phase into sequential build steps is the house
convention rather than an invention here. Two alternatives were rejected: **M6**
collides — the package's M6 is Communications and this repo treats that document
as canonical (ADR 0007). And **B1**, the package's own name for credits, is wrong
in both directions at once: it includes `referrals` and `referral_unlocks`, which
M5b does not build, and excludes the opening-balance ETL, which M5b does. `M5a` /
`M5b` says what actually happened.

---

## What was measured, against what the package claims

Six of these contradict or extend `docs/edufurther-migration/`. Where they
disagree, the measurement wins — the package is a brain dump (ADR 0007), and dev
*structure* is real while dev *content* is junk.

| # | Finding | Consequence |
|---|---|---|
| 1 | **Legacy `Reviews` has no session link.** Confirmed three ways: `docs/bubble-data-model.md:181-199`, the Data API's `⭐review`, and `script-data-dev/review.json` | The runbook's "link by `reviewedBy` + `reviewedFor` + proximity" has no anchor. 8 of 12 dev mentee↔mentor pairs have more than one booking |
| 2 | **The four mentor ratings are a 3-point scale stored as `1.67 / 3.34 / 5`** — `1, 2, 3` multiplied by `5/3`. The package declares them `int CHECK (BETWEEN 1 AND 5)` | **An `int` column cannot hold `3.34`.** The load would fail or truncate silently. This is the first measured contradiction that breaks the schema outright |
| 3 | **`npsRecommendScore` is 1–10**, confirmed by the UI and a dev value of `8`. The package permits `0` | The `CHECK` tightens to `1..10`. Nothing produces `0` |
| 4 | **The export renders every value as a string** (`'3.34'`); the API returns int/float | The transform parses. Same export/API asymmetry already documented for other types |
| 5 | **`Creator` equals `reviewedBy`** in the one dev row | `reviewedBy` is the author anchor; `Creator` needs no fallback. One row, so re-check at the production export |
| 6 | **Bubble Data API type names carry emoji** — the type is `⭐review`, and only `user` has no prefix. `GET /api/1.1/meta` lists them verbatim | Plain-name probes 404. Use `/meta` before any extract |
| 7 | **The LIVE Data API exposes zero types**; test exposes 16 | Not a blocker — loaders read Data-tab exports. It *is* a cutover blocker for anything only the API carries |
| 8 | **Dev holds one review; production holds 53** | Enough to pin the transform's shape, not enough to trust its content |

---

## Decisions

### Shape

1. **Four mentor ratings are `smallint CHECK (BETWEEN 1 AND 3)`** — the ordinal
   the mentee actually chose. `1 = Not great`, `2 = Great`, `3 = Excellent`.
   Percentages and the `X/5` are **derived**, never stored (handoff principle 3).

   *Why not `numeric(3,2)` carrying `1.67/3.34/5`:* nobody ever chose `3.34` —
   it is Bubble's presentation scaling baked into storage. A `numeric` column
   also permits `2.15`, which the product cannot produce and cannot interpret,
   which is the same defect as an enum value with no producer. The migration
   maps `1.67→1`, `3.34→2`, `5→3`, losslessly and reversibly, and
   `legacy_bubble_id` keeps the join back.

   *A deliberate departure from #100.* These are a closed set by #100's own test,
   which would make them `text` + `CHECK`. They stay `smallint` because the
   display must **average** them and text moves that mapping into every query.
   The `StrEnum` still exists at the Pydantic boundary, so the API publishes
   `"excellent"` rather than a magic number.

2. **`valuable_rating` is `1..5`; `nps_recommend_score` is `1..10`.** Both are
   genuine point scales, unlike the four above.

3. **`session_id` is nullable in the column and required at the boundary.** The
   53 legacy reviews have no session and `NOT NULL` cannot hold them; every new
   review has one. This is the #82 shape — `sessions.session_type_id` shipped
   nullable with `NOT NULL` as a later contract migration.

4. **One FK, not two.** The offering is reached by joining
   `sessions.session_type_id`. A `session_type_id` column on `reviews` would be
   the same fact twice (non-negotiable #8), and unlike decision #10's
   snapshotted rate it is not a mutable value.

5. **Migrated rows take `session_id = NULL` and `reviewed_for_role = 'mentor'`**
   (renamed by decision 17) —
   the legacy table is "storing mentors reviews". Fabricating a link from
   `reviewedBy` + `reviewedFor` + proximity is irreversible; a later backfill is
   additive.

### Behaviour

6. **Append, never overwrite.** A new session earns a new review. The legacy app
   updated one row per pair; that is not carried forward.

   *Why:* a review is about a *session*, so editing session A's row because the
   mentee attended session B leaves a row claiming to be about A while
   describing B. The package already assumes append —
   `UNIQUE (session_id, reviewed_by)` is one review *per session*. The display is
   a dated list, which shows a trajectory under append and silently rewrites
   history under edit. And this project already ruled on this shape once: D8
   chose a ledger over a counter because a counter leaves "no record". A mentor
   whose quality dropped is the same argument.

   The concern edit solves — one mentee dominating an average — is a **display**
   problem. Cap or recency-weight in the aggregate; do not destroy rows.

7. **The 30-day window suppresses the request *and* refuses the write.** If this
   mentee reviewed this **offering** less than 30 days ago, no request is sent
   and a `POST` is refused. Past the window, a new booking earns a fresh request.

   **Scoped to the offering, not the mentor — narrowed 2026-08-22, and the
   original scope was a real defect rather than a preference.** Per mentor, this
   rule collides with decision 6: two sessions inside a month yield *one*
   review, and the second is never even asked for, because the request producer
   suppresses on this same predicate. A mentor strong at CV review and weak at
   interview prep is two facts, and a mentor-wide window keeps whichever was
   booked first and silently discards the other.

   The protection that mattered survives: *one review per session* still caps a
   single mentee at one review per hour they actually attended, and sessions
   cost credits. What is given up is the case of one mentee reviewing the same
   offering repeatedly — which is what the window was always really about.

   *One rule, not two.* Suppressing only the request would leave the profile
   picker free to accept what the producer declined, which is two
   representations of one rule and the shape #8 calls a defect. It is also what
   the legacy behaviour was protecting: the mentee reviews a given mentor at most
   once per 30 days, by any path.

8. **One eligibility predicate, written once and reused** by the write, the
   picker and the request producer: *completed · the caller's own · this session
   not already reviewed · this offering not reviewed inside 30 days*. Two rules
   for the same question would eventually disagree (#8), and the disagreement
   would be a mentee invited to review a session the write then refuses —
   `test_the_picker_and_the_write_agree` is what pins them together.

   **A session with no offering escapes the interval clause**, which is correct
   rather than a hole: `sessions.session_type_id` is nullable for migrated rows,
   so there is nothing to compare, and the one-per-session rule still caps them
   at one apiece. Refusing them instead would make every migrated session
   permanently unreviewable.

9. **`session_id` reaches the write by one of two paths**, and both supply it:

   | Path | Source | Ambiguity |
   |---|---|---|
   | Review request by email, later WhatsApp | The request is per-session, so the signed link carries it | None, ever |
   | Mentor profile Reviews tab | Resolved silently when one eligible session exists; a picker when more than one | Asked only when it exists |

10. **The request fires on the transition into `completed`, not on a clock.**
    `settle_sessions` decides `completed` vs `missed`; a timer set to "end + 10
    minutes" races that sweep and could ask for a review of a session nobody
    attended. Firing on the transition makes the delay automatic and makes a
    `missed` session structurally incapable of triggering a request.

11. **Two notification members, both `Audience.MENTEE`:** `REVIEW_REQUESTED` and
    `REVIEW_REMINDER` at 24 hours, cancelled by the review existing. This retires
    the note in `domain/notifications.py` saying reviews have no producer.

12. **`PATCH /reviews/{id}` inside a 10-minute edit window.** Author only, all
    fields editable — it is a compose grace period, not an amendment, so no
    revision history is kept (nothing would read it, #21).

    **The 30-day clock runs from `created_at`, never `updated_at`**, so an edit
    cannot extend the suppression window. Refusal past the window is `409`,
    following #110; `404` stays reserved for a review that is not the caller's,
    because `403` confirms the row exists.

13. **Both windows are domain constants, not configuration** —
    `REVIEW_INTERVAL = timedelta(days=30)` and
    `REVIEW_EDIT_WINDOW = timedelta(minutes=10)`, in `domain/`, beside
    `CANCELLATION_CUTOFF` and `RESPONSE_WINDOW`, which are the same shape.

    **Config was asked for and is the wrong home.** `core/config.py` holds no
    product rule today — every "how long" in this codebase is either a domain
    constant or a column's server default (#104's `min_notice_minutes`). A
    product rule in env config can differ *per environment*, so staging and
    production would disagree about when a review may be written and the
    resulting bug is unreproducible. The review asymmetry matters too: a
    constant changes by a one-line PR through the gate, an env var by a
    dashboard edit with no test and no reviewer.

    **If `REVIEW_INTERVAL` later needs real runtime tuning**, the precedent is
    #104 — a column with a server default, additive whenever it is wanted. Not
    an env var.

### Settled while building, 2026-08-22

Four decisions the plan did not carry. They are recorded here rather than
argued again in each PR that acts on them.

14. **The discovery card carries `review_count` and `session_value`, and
    nothing else.** `session_value` is the mean of `valuable_rating`, over 5 —
    the figure the profile already shows as `5/5 Session Value`. Not the four
    ordinals, which only read as percentages, and not the NPS, which is a
    likelihood rather than a quality score.

    Legacy carried `countReviewReceived` on *Mentor (front search)* and no
    rating, so the count is restored and the rating is new. **Lands in PR 3.**

15. **A rating never enters the keyset sort.** Browse pages on
    `mentor_profiles.id` (ADR 0016's base case), so ordering by rating would
    invalidate the cursor — equal ratings swap between pages, showing one mentor
    twice and hiding another.

    **And no sort-by-rating control is wanted.** A raw rating order puts one
    5/5 above two hundred reviews averaging 4.8, which needs a weighted score to
    be honest at all. If rating ever influences ordering it joins `_ranked()`'s
    formula on the **offset** path, which that docstring already anticipates by
    name. A `?min_rating=` *filter* is additive and can arrive whenever it is
    wanted — `ix_reviews_mentor_valuable` is already there to serve it.
    **Lands in PR 3.**

16. **The form's required-ness is the schema's.** Measured from the three
    screens in `FE-ui-guide/reviewUI/` — note the file names invert the order,
    `reviewquestion3.png` is step 1:

    | Step | Field | Required |
    |---|---|---|
    | 1 | the four mentor ratings, `Not great / Great / Excellent` | all four |
    | 2 | `valuable` 1–5, `recommend` **1–10**, **Public review** | all three |
    | 3 | **Improvement Feedback** | **no — the only optional field** |

    So every column is `NOT NULL` except `private_review`. The canonical DDL
    leaves `public_review` nullable and the screen overrules it. Whether all 53
    legacy rows carry one is unverified until the export; the constraint is
    deliberately what will say so, loudly, at rehearsal.

    This also confirms finding #3 at the control rather than by inference: the
    recommend row renders ten buttons starting at one, so **nothing can produce
    a `0`**.

17. **`reviewed_for_role`, not the package's `reviewed_role`, and `deleted_at`
    ships with the table.**

    The name is the explicit form of what the column is: `reviewed_for` names a
    *user*, and a user is a mentor to one person and a mentee to another. The
    role is the capacity they were reviewed in. It is **not** the same fact
    twice — unlike decision 4's offering, it cannot be reached by a join, because
    the 53 migrated rows have no session to join to. It reuses `SessionRole`
    rather than inventing a vocabulary, so #21 does not apply.

    `deleted_at` is in the canonical DDL and had no equivalent in this plan.
    `sessions` deliberately carries none — a cancelled session is still a
    session — but published review text is the one thing here that will need
    moderation, and hard-deleting evidence contradicts decision 6's ledger
    argument as directly as an overwrite would.

    **The rename is a divergence from a canonical document, so it will drift if
    it is not looked for.** `02_FIELD_MAPPING.md` §6 says `reviewed_role`, and
    that file is never edited here (ADR 0007). Whoever writes PR 5 will read the
    mapping and the table side by side; this row is the only thing that
    reconciles them. The package's own reason for the column — *"dual roles
    shouldn't blend reputations"* — is the same one kept here.

18. **Withdrawal removes a review from what is *published*, not from what
    *happened*.** The three rules therefore treat `deleted_at` differently, on
    purpose:

    | Rule | Excludes withdrawn? | Why |
    |---|---|---|
    | The card and profile aggregates | **yes** | A moderated review must not move a mentor's average |
    | The 30-day window | **no** | Otherwise moderation hands the author a fresh review slot immediately — the incentive exactly inverted |
    | One review per session per author | **no** | The slot was used. A withdrawn review is still a record that the session was reviewed |

    So `ix_reviews_mentor_valuable` carries `deleted_at IS NULL` and the other
    two carry no such predicate, and
    `test_the_eligibility_index_orders_by_recency` asserts the **absence**
    — which is what stops the next reader adding it for symmetry.

    The cost of getting it wrong was measured, not assumed: adding
    `AND deleted_at IS NULL` to the eligibility query drops it from an Index
    Only Scan with zero heap fetches to an Index Scan with a heap filter.

    ~~**A debt PR 3 has to pay.**~~ **Paid in PR 2, and this row named the wrong
    pull request.** The expiry test globs `src/app/infra/db/*.py`, so the moment
    *any* store module names `reviews` the exemption lapses — and PR 2's
    `review_reader` did it one pull request earlier than "the aggregates land"
    predicted. It fired on the full gate exactly as designed and named its own
    fix.

    The case it took is `PATCH`'s read-back rather than an aggregate, which is
    the more useful one: it is the only path where withdrawal changes an answer,
    because a review taken down by moderation is not editable back into
    existence.

19. **The two `409`s on `POST /reviews` carry a machine-readable `type`, and
    settled decision #110 is where that was already decided.** That row shipped a
    `409` without one because there was a single refusal to distinguish, and
    recorded the condition for revisiting: *"A second refusal exists. Then the
    409 needs a machine-readable reason and the `type` slot in the RFC 9457
    envelope is where it goes — additive, so nothing shipped here blocks it."*

    This surface is that condition, twice over:

    | `type` | Means | What a client does |
    |---|---|---|
    | `/problems/review-already-exists` | this session already carries your review | never retry |
    | `/problems/review-interval-not-elapsed` | you reviewed this offering recently | retry after the window |

    Without them the two are one status code and two English sentences, and a
    client either retries forever or abandons a review it could have written next
    month. **Relative URIs**, because an absolute one names either a host that
    differs per environment or a documentation site that has to exist.

    `GET /me/reviewable-sessions` is the other half: ask it first and a `409`
    becomes an exceptional race rather than the routine way to discover a rule.
    It returns `Page`, like every list in this API — a bare array has nowhere to
    put pagination the day one is needed, and adding it later breaks every
    client already parsing the array.

    **And the `409` is a real race, not only a rule.** Two taps both pass the
    pre-check before either commits; the second insert blocks on
    `uq_reviews_one_per_session_author` and fails when the first lands. The
    writer catches it by constraint name and raises the same error the check
    would have, so the two paths are indistinguishable to a client. That is
    settled decision #169, and `book_session` is where the shape came from.

---

## Register — what is still open

| # | Question | Blocks |
|---|---|---|
| 1 | **The 53 production reviews, as a Data-tab export.** Confirms every rating is one of `1.67/3.34/5`, that no NPS is `0`, and that every row carries public text. No deploy needed | PR 5's **rehearsal**, no longer PR 1 |

The `CHECK` is the safety net for question 1: shipping `1..3` means an
unexpected production value fails the load **loudly at rehearsal**, which is the
desired failure mode rather than a risk. It no longer blocks the loader either —
the transform is built and tested against the one dev row plus synthetic fixtures
for the cases it does not contain, and the export becomes the gate before the
real load rather than before the code.

**Closed 2026-08-22:** "Not great" renders **33%**, not 0%. The display shows
`97% Recommended` over three reviews, which no promoter fraction can produce —
three people yield 0/33/67/100 — and which a normalised `(v-1)/9` would render
as 96%. The app scales `mean/max`, so the floor of a three-point scale is 33%.
The read publishes `count`, `average` **and** `percent`, derived server-side and
pinned by a test, because the alternative is the client owning the mapping.

**Closed 2026-08-20:** the 30-day window refuses the write as well as suppressing
the request (decision 7), and the edit window is 10 minutes (decision 12). Both
are domain constants rather than configuration (decision 13).

---

## Sequence

| # | PR | Migration | Tier |
|---|---|---|---|
| 1 | `reviews` — the table, corrected scales, eligibility and card indexes ✅ | yes | 1 |
| 2 | `POST` and `PATCH /reviews`, and `/me/reviewable-sessions` — the interval in the query ✅ | no | 2 |
| 3 | Mentor reviews read — aggregates and the dated list | no | 2 |
| 4 | `REVIEW_REQUESTED` + `REVIEW_REMINDER` producers | no | 2 |
| 5 | The reviews loader — built on the dev row, rehearsed on the 53 | no | 1 |
| 6 | ADR — the scale decision, the append rule, the 30-day window | no | 3 |

One ADR, not three. Only the scale decision clears rule 3's bar on its own —
expensive to reverse *and* surprising — and the append rule and 30-day window are
the behaviour it exists to support, so they belong in the same record.

---

## For the FE

Three things found while measuring `reviewUI/`, none of them backend work.

| # | What | Where |
|---|---|---|
| 1 | **The recommend question's scale labels are wrong.** "How likely are you to recommend this mentor to others?" renders `Not valuable at all` / `Extremely valuable`, copied from the question directly above it. It should read `Not at all likely` / `Extremely likely`. The scale itself is right — `1..10`, no zero | step 2 |
| 2 | **The percentage belongs to the API, not the client.** The read publishes `count`, `average` and `percent` together, derived server-side and pinned by a test. A client that recomputes `percent` from `average` creates the second copy of one mapping that non-negotiable #8 calls a defect | profile Reviews tab |
| 3 | **The screenshot file names invert the order** — `reviewquestion3.png` is step 1 and `reviewquestion1.png` is step 3. A reading hazard, not a product bug | `FE-ui-guide/reviewUI/` |

Item 2 is the one that matters: without it the FE will reasonably assume the
percentage is theirs to compute.

---

## Adjacent, and not this phase

**`auth_identities` has no operational function.** Sign-in resolves through
`users.auth_id`; Supabase auto-links a provider to a confirmed email; new users
never get a row; the ETL is its only writer. ADR 0009 §8 exempted the table from
the test §7 had just applied one paragraph above.

It is inert, so removing it is not worth its own PR. **Do it when M1 is next
touched**, which is the phase that owns these tables and where the deletion is
close to free.
