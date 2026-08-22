# Handoff — the review build (M5a)

**Status:** planned, nothing built. Six PRs, one ADR.
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

5. **Migrated rows take `session_id = NULL` and `reviewed_role = 'mentor'`** —
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
   mentee reviewed this mentor less than 30 days ago, no request is sent and a
   `POST` is refused. Past the window, a new booking earns a fresh request.

   *One rule, not two.* Suppressing only the request would leave the profile
   picker free to accept what the producer declined, which is two
   representations of one rule and the shape #8 calls a defect. It is also what
   the legacy behaviour was protecting: the mentee reviews a given mentor at most
   once per 30 days, by any path.

8. **One eligibility predicate, written once and reused** by the request
   producer and the profile picker: *completed · with this mentor · not already
   reviewed · no review of this mentor inside 30 days*. Two rules for the same
   question would eventually disagree (#8).

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

---

## Register — what is still open

| # | Question | Blocks |
|---|---|---|
| 1 | **The 53 production reviews, as a Data-tab export.** Confirms every rating is one of `1.67/3.34/5` and that no NPS is `0`. No deploy needed | PR 1's `CHECK` bounds |
| 2 | Does "Not great" render **0%** (ordinal) or **33%** (scaled)? Decides whether the read publishes the ordinal or the percentage | PR 3 |

The `CHECK` is the safety net for question 1: shipping `1..3` means an
unexpected production value fails the load **loudly at rehearsal**, which is the
desired failure mode rather than a risk.

**Closed 2026-08-20:** the 30-day window refuses the write as well as suppressing
the request (decision 7), and the edit window is 10 minutes (decision 12). Both
are domain constants rather than configuration (decision 13).

---

## Sequence

| # | PR | Migration | Tier |
|---|---|---|---|
| 1 | `reviews` — the table, corrected scales, eligibility indexes | yes | 1 |
| 2 | `POST` and `PATCH /reviews` — session-scoped, 30-day rule in the query | no | 2 |
| 3 | Mentor reviews read — aggregates and the dated list | no | 2 |
| 4 | `REVIEW_REQUESTED` + `REVIEW_REMINDER` producers | no | 2 |
| 5 | The reviews loader — 53 rows | no | 1 |
| 6 | ADR — the scale decision, the append rule, the 30-day window | no | 3 |

One ADR, not three. Only the scale decision clears rule 3's bar on its own —
expensive to reverse *and* surprising — and the append rule and 30-day window are
the behaviour it exists to support, so they belong in the same record.

---

## Adjacent, and not this phase

**`auth_identities` has no operational function.** Sign-in resolves through
`users.auth_id`; Supabase auto-links a provider to a confirmed email; new users
never get a row; the ETL is its only writer. ADR 0009 §8 exempted the table from
the test §7 had just applied one paragraph above.

It is inert, so removing it is not worth its own PR. **Do it when M1 is next
touched**, which is the phase that owns these tables and where the deletion is
close to free.
