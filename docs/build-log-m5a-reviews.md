# Build log — M5a, reviews

**Status: M5a is complete.** The table, the write surface, the read, the two
messages that ask for a review, and the loader. Nothing in it is blocked; two
things outside it are, and both are named at the bottom.
**Written:** 2026-08-24, from the merged pull requests rather than from memory.
**Plan:** `handoff-review-build.md`, whose numbered sequence this executed.
**Next:** M5b — credits. The split and its reasoning are in the handoff.

**A record, not a plan.** The `handoff-*` documents say what is going to be built
and why; this says what was, what it decided along the way, and what it found.
Written for whoever picks this up next, because the PR titles say what shipped
and nothing else says why the shape is what it is.

Settled decisions live in `.claude/skills/project-conventions/SKILL.md`; this
points at them rather than repeating them.

---

## What shipped

Merged, in order:

| PR | |
|---|---|
| #197 | the `reviews` table, and the scale the package cannot hold — with ADR 0026 |
| #199 | a mentee reviews a session, once |
| #201 | the index column nothing reads, and the header every other create sends |
| #202 | what a mentor's reviews add up to |
| #203 | a finished session asks the mentee how it went |
| #204 | the reviews loader |
| #205 | the template keys an operator must set, and the shape the FE will get |

Two pull requests were closed and re-landed under new numbers — #198 → #199 and
#200 → #202 — both because a stacked branch's base was deleted on merge and
GitHub closed the child. See *the stack cost more than it saved*, below.

---

## The one measurement the phase turned on

The legacy `Reviews` type stores a **three-point** scale as `1.67 / 3.34 / 5` —
the ordinals `1, 2, 3` multiplied by `5/3` so they render on a five-point
display. The migration package declares those columns `int CHECK (BETWEEN 1 AND
5)`.

**An `int` column cannot hold `3.34`.** That is the first measured contradiction
in the package that does not merely disagree with the product but fails to load
its own data, and it is why this phase has an ADR.

The trap underneath it is worse than the contradiction. `3.34` assigned to a
`smallint` **rounds to 3** and satisfies `CHECK (BETWEEN 1 AND 3)`, so a
transform that casts rather than maps stores *Excellent* where the mentee chose
*Great* — every row loaded, every gate green, and nothing anywhere saying the
data is wrong. The column cannot be the guard. Settled decision **#168** records
it and `test_a_scaled_legacy_value_rounds_instead_of_failing` is its executable
form, sitting in the schema tests where a loader author meets it before writing
a cast.

---

## Decisions taken, and why

**The interval is per offering, not per mentor.** The plan scoped the 30-day
window to the mentor. That collides with the append rule: two sessions inside a
month yield *one* review, and the second is never even requested, because the
producer suppresses on the same predicate. A mentor strong at CV review and weak
at interview prep is two facts. Costs nothing in the schema — the offering is
reached by joining `sessions`, so "one FK, not two" stands.

**One eligibility predicate, three callers.** The write refuses on it, the picker
lists by it, the request producer suppresses on it. It was written to take its
user as a parameter, which is why the third caller needed no reshaping at all —
`within_interval(Session.mentee_id, Session.session_type_id, now)` composes
set-based inside the settlement sweep. `test_the_picker_and_the_write_agree`
pins the pair.

**The request fires on the transition into `completed`, never on a clock.**
`settle_attendance` decides `completed` against `no_show` from attendance, so a
session nobody turned up to is *structurally incapable* of asking for a review. A
timer set to "end plus ten minutes" would race that sweep and sometimes win.

**The reminder is a sweep, and that reversed a decision made on the house
pattern.** The first build scheduled one QStash message per settled session,
following `book_session`. Measured over 2,000 settled sessions: **2,000
sequential HTTP calls**, turning a 4-second sweep into a two-minute one, and the
`Scheduler` port has no batch call. A query costs one round trip at any volume.
Anchoring it on the `REVIEW_REQUESTED` outbox row rather than on the settlement
made the suppression free — a session whose request was suppressed has nothing
to nudge from.

**The card's query is narrower than the profile's.** `ix_reviews_mentor_valuable`
covers `(reviewed_for, valuable_rating)`, so the card's two figures plan as an
Index Only Scan with zero heap fetches. Reusing the profile's wider summary —
the *tidier* code — drops the same page to a Bitmap Heap Scan touching 279 heap
blocks. Settled decision **#170**.

**Withdrawal means three different things, on purpose.** The aggregates exclude a
withdrawn review; the interval and the one-per-session rule do not. If moderation
reset the window, an author whose abusive review was taken down could write a
replacement immediately. So `ix_reviews_mentor_valuable` carries `deleted_at IS
NULL` and the other two carry no such predicate — and a test asserts the
**absence**, which is what stops the next reader adding it for symmetry.

**The 53 migrated rows take `session_id = NULL`.** The runbook says to link by
`reviewedBy` + `reviewedFor` + proximity; that has no anchor, since 8 of 12 dev
pairs have more than one booking. A fabricated link is irreversible where a
backfill is additive. This is a stated departure from a canonical document.

---

## Defects found, and what found them

The useful part of this log. Every one of these passed a full green gate.

| what | how it was found |
|---|---|
| **the percentage rounded ties the wrong way** — a Python `100.0` made the arithmetic `float8`, and `round(float8)` is half-to-**even**, the opposite of the docstring, the CHANGELOG and the drift test | `/code-review`, which reproduced `round(62.5)` giving 62 against numeric's 63 |
| the same bug, older, in `attendance_rate` — five of eight attended published as 62% | the review above, noticing the identical shape |
| **a soft-deleted reviewer stayed named on a tokenless public endpoint** — first name, initial and institution, forever | `/code-review`; `predicates.LIVE` exists because this rule had been missed twice already |
| a double-tapped `POST /reviews` returned **500** — check-then-insert, and `IntegrityError` is not an `AppError` | reviewing the write path for races |
| `GET /me/reviewable-sessions` returned a bare array — the only one in the API, against ADR 0016 | grepping the routes for the convention rather than assuming |
| `--dry-run` **always exited 0**, while its own docstring promised the opposite — on the rehearsal that runs before the freeze | `/code-review` |
| the reconciliation's "nothing was lost" check **could never fire** — it filtered non-empty anchors for falsy ones, and both sides came from the same plan | `/code-review`; `reconcile.py` records this exact defect shipping once before |
| the loader **quarantined every row** — it read the export's field spellings, and `JsonExportSource` canonicalises them | running the dry run for the first time |
| ratings paired **positionally** against a constant documented as *the order the form asks them* — a UI ordering | `/code-review` |
| `encode_id_cursor` paired with the two-part `decode_cursor` — every cursor the endpoint issued was rejected by the endpoint | a paging test that fetched page two |
| the dev-export test hard-failed in any checkout — `script-data-dev/` is gitignored | `/code-review`, running the gate on a fresh clone |

**Three tests were green while proving nothing, and they share one shape: the
fixture could not reach the case.**

- The rounding pin recomputed with `[1, 2, 3]`, which cannot produce a `.5`.
- `test_somebody_elses_review_cannot_be_edited` minted a token for an `auth_id`
  with no `users` row, so it was refused at *authentication* — it would have
  passed with the authorization scope removed entirely.
- Nothing asserted the reconciliation could fail, which is why one that could
  never fail shipped.

No gate can catch this. A passing test looks identical whether it exercises its
subject or not, and coverage counts the line either way. The only thing that
found all three was a reader asking *does this test test what it says* — which
is the argument for `/code-review` on anything with real data behind it, and the
reason two of this phase's five build PRs got one.

---

## The stack cost more than it saved

How-we-work rule 1 permits stacking and names the price: *"every stack so far
needed rebase surgery."* This phase paid it twice.

#198 was stacked on #197. When #197 merged, its branch was deleted, GitHub found
no base to retarget to, and **closed #198** — `CONFLICTING`, with the work
intact but unreviewable. The same thing then happened to #200 on merging #199.

Neither recovery was hard, and neither was free: rebuild the branch onto `main`
with the child's content only, verify the tree is byte-identical to the old tip,
re-gate, re-push, reopen under a new number. Roughly an hour each, producing no
new functionality.

**The house already has the remedy and a naming convention for it** — #195,
#194, #191, #190 and #187 are all `…-onto-main` branches from the same
situation. Every PR from #201 onward branched off `main` and none of them
conflicted, because the files were genuinely disjoint.

The lesson is not "never stack". It is that a stack is worth it only when the
child *cannot* be written without the parent — and for four of these five, that
was never true.

---

## The numbering collision is real

Settled decision #179 records that parallel branches each append "the next
number" from the table as it stood when they were cut, and nobody spots a
duplicate in a 170-row document.

This phase hit it live: #202 had already claimed **170** while #201 was in
flight, so #201 took **171** and says so in the row. Taking the next number
*visible from the branch* would have produced the duplicate its own test exists
to catch.

Six rows were added or amended: **#162** (amended — the dedup index is partial
on `payload ? 'kind'`, so a producer omitting the kind sits outside it),
**#168**, **#169**, **#170**, **#171**, **#172**.

---

## What is still missing

**Two Loops templates.** `review_requested` and `review_reminder` have no entry
in `EMAIL_TEMPLATES`. `template_for()` raises rather than falling back, so until
both are mapped a settled session queues a request that fails at the **drain** —
the outbox keeps the row, so nothing is lost and nothing arrives either.
`.env.example` names every key and warns that the value replaces the whole map.

**The 53-row production export.** Register question 1, open since the plan was
written. It gates the loader's *rehearsal*, not its code: the transform runs
against the one dev row today. Confirmed that all 53 carry public text; if any
turn out empty the answer is to relax the column rather than quarantine, since
they are real reviews — a contract migration of the `session_id` shape, nullable
in the column and required at the boundary.

**Three items for the FE**, in `handoff-review-build.md`. The one that matters is
that the percentage belongs to the API: both `average` and `percent` are
published and pinned to each other, so a client that recomputes creates the
second copy of one mapping. That note described a draft shape until #205
corrected it against `model_fields`.

**`session_id NOT NULL`.** The column is nullable because the 53 legacy rows have
none. Once they are loaded and nothing contradicts it, the contract migration is
additive — the same path `sessions.session_type_id` took.

**A rating filter on discovery.** Additive whenever it is wanted;
`ix_reviews_mentor_valuable` already serves it. **No rating *sort*** was built,
deliberately: browse pages by keyset on `mentor_profiles.id`, and a raw rating
order puts one 5/5 above two hundred reviews averaging 4.8. If rating ever
influences ordering it joins `_ranked()`'s formula on the offset path.
