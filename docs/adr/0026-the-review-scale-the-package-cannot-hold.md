# 26. Reviews store the ordinal a mentee chose, not the number Bubble displayed

Date: 2026-08-22

## Status

Accepted.

Diverges from `docs/edufurther-migration/`, which ADR 0007 makes canonical for
the target data model. `05_credits_reviews.sql` specifies the four mentor
ratings as `int CHECK (BETWEEN 1 AND 5)`, `nps_recommend_score` as
`CHECK (BETWEEN 0 AND 10)`, `public_review` as nullable, and the role column as
`reviewed_role`. None of the four survives. The package is not edited here
(ADR 0007); this record is the departure.

**One of the four is forced and three are chosen**, which is why this is a
record rather than a bug report. An `int` column cannot hold `3.34`, so *a*
change is unavoidable — but not *this* change, and the other three divergences
are against declarations that would have worked.

## Context

The legacy `Reviews` type asks four questions about the mentor on a three-point
scale rendered `Not great / Great / Excellent`. **Bubble stores those three
answers as `1.67 / 3.34 / 5`** — the ordinals `1, 2, 3` multiplied by `5/3` so
they render on a five-point display. Measured on `script-data-dev/review.json`
and confirmed against the three review screens in `FE-ui-guide/reviewUI/`.

The package declares those columns `int`. **An `int` column cannot hold
`3.34`**, so the canonical DDL breaks on the first migrated row — 53 of them in
production. This is the first measured contradiction in the package that does
not merely disagree with the product but fails to load its own data.

Three smaller measurements point the same way. The recommend control renders ten
buttons starting at `1`, so **nothing can produce the `0` the package permits**.
The form labels the public review *"(required)"* and will not submit without it,
where the package leaves the column nullable. And `reviewed_role` names the
capacity a user was reviewed in, which `reviewed_for` — a plain user id — cannot
say on its own; the package's own decision register gives the reason, *"dual
roles shouldn't blend reputations"*, and the name does not say it.

The 53 legacy reviews carry **no session link at all**, confirmed three ways:
`docs/bubble-data-model.md`, the Data API's `⭐review` type, and the dev export.
So `session_id` cannot be `NOT NULL`, and the legacy app's habit of keeping one
row per mentee/mentor pair and updating it in place cannot be carried forward
either — a review is about a session, and 8 of 12 dev pairs have more than one.

## Decision

**The four mentor ratings are `smallint CHECK (BETWEEN 1 AND 3)`** — the ordinal
the mentee actually chose. `1 = Not great`, `2 = Great`, `3 = Excellent`. The
migration maps `1.67 → 1`, `3.34 → 2`, `5 → 3`, and `legacy_bubble_id` keeps the
join back to the row that carried the scaled value, so the mapping is reversible.

Percentages and the `X/5` a profile shows are **derived at query time and never
stored**, per D56.

**The wire publishes words and the column stores numbers.** `MentorRating` is a
`StrEnum` at the Pydantic boundary — `not_great`, `great`, `excellent` — and the
stored ordinal is the member's *position*, so the mapping is declared once. This
is settled decision #100's reasoning applied at the only layer where it costs
nothing: the display must **average** these, and `text` in the column would move
the mapping into every query.

Three consequential narrowings ride with it:

- `nps_recommend_score` is `1..10`, not `0..10`
- `public_review` is `NOT NULL`
- the role column is **`reviewed_for_role`**, not `reviewed_role`

And two behaviour rules that exist to make the shape mean something:

- **Append, never overwrite.** A new session earns a new review;
  `UNIQUE (session_id, reviewed_by)` — partial on `session_id IS NOT NULL`, so
  the 53 sessionless rows are not collapsed into one another — is that rule as a
  constraint.
- **One review per offering per 30 days**, by any path: the request producer and
  the write share one predicate.

## Consequences

**The `CHECK` is not the loader's guard, and believing otherwise is the trap.**
`3.34` assigned to a `smallint` **rounds to `3`** and satisfies
`BETWEEN 1 AND 3`. A transform that casts rather than maps stores *Excellent*
where the mentee chose *Great* — every row loaded, every gate green, and nothing
anywhere saying the data is wrong. The mapping must be explicit in the
transform. Settled decision #169 generalises this; `test_a_scaled_legacy_value_
rounds_instead_of_failing` is its executable form.

What the `CHECK` *does* catch is a value the dev export does not contain. Dev
holds one review and production holds 53, so the tight bound is what turns an
unexpected fourth value into a loud failure at rehearsal rather than a silent
one at load.

**The rename costs a permanent reconciliation.** `02_FIELD_MAPPING.md` §6 says
`reviewed_role` and is never edited here. Whoever writes the loader reads the
mapping and the table side by side, and only this record and handoff decision 17
reconcile them. That is the price of the clearer name, paid knowingly.

**`session_id` is nullable in the column and required at the boundary.** This is
the shape `sessions.session_type_id` already shipped in: the column admits what
history contains, the contract admits only what the product writes, and `NOT
NULL` becomes a later contract migration once no row contradicts it.

**Reversing this is a migration plus a re-derivation.** Going back to the
package's `int 1..5` means rewriting every stored ordinal through
`legacy_bubble_id` for migrated rows and through the `StrEnum` for new ones. It
is reversible, and it is not cheap — which with the surprise of a `1..3` column
under a canonical `1..5` declaration is what puts this over how-we-work rule 3's
bar and makes it a record rather than a settled-decision row.

### Confirmation

How you would know this is being honoured, and what nothing checks.

| claim | what checks it |
|---|---|
| the scale is three points, both ends | `test_a_mentor_rating_off_the_three_point_scale_is_refused` and `test_every_point_on_the_three_point_scale_is_accepted`, written straight to the column |
| the ordinal matches the word | `test_the_scale_is_pinned_to_its_ordinals` asserts the three pairs, not a round trip — a round trip passes against any consistent ordering, including a wrong one |
| the vocabulary and the bound agree | `test_the_scale_has_exactly_the_points_the_column_allows` |
| **the `CHECK` cannot catch `3.34`** | `test_a_scaled_legacy_value_rounds_instead_of_failing` asserts the rounding, so the trap is executable rather than a paragraph |
| no `0` reaches the recommend column | `test_a_recommend_score_of_zero_is_refused` at the column, a parametrised `422` at the boundary |
| public text is required | `test_a_review_without_public_text_is_refused`, plus `""` and `"   "` at the boundary |
| the migrated rows survive the uniqueness rule | `test_the_migrated_rows_are_not_collapsed_by_the_unique_index` |
| append rather than overwrite | `test_a_second_session_earns_a_second_review` |
| the interval holds, and lifts | both directions, plus `test_an_edit_never_postpones_the_next_review` |
| the wire speaks words | `test_the_answers_come_back_as_words` |

**Blind spots, stated rather than implied.**

- **Nothing has yet seen the 53 production reviews.** Every claim about their
  content is inferred from one dev row and three screenshots. The `CHECK` is the
  safety net, and the failure mode is deliberately loud, but the export is still
  the first real evidence.
- **Nothing prevents a reordering of `MentorRating` from silently changing what
  every stored row means.** `test_the_scale_is_pinned_to_its_ordinals` is the
  only guard, and it is a test rather than a type: the ordinal is a position, so
  a reorder produces no type error and needs no migration.
- **Nothing compares this table to `02_FIELD_MAPPING.md`.** The `reviewed_role` /
  `reviewed_for_role` divergence is reconciled in prose here and nowhere in code,
  so it survives only as long as somebody reads this record.
- **Nothing enforces that `reviewed_for` equals `sessions.mentor_id`.** A `CHECK`
  cannot span two tables; the invariant lives at the single write path, asserted
  by `test_the_mentor_is_taken_from_the_session_not_the_request`. A second writer
  would be able to break it.

## Alternatives considered

**`numeric(3,2)` carrying `1.67 / 3.34 / 5` as stored.** Faithful to the export
and the smallest possible transform. Rejected because **nobody ever chose
`3.34`** — it is Bubble's presentation scaling baked into storage, and the column
would also admit `2.15`, which the product can neither produce nor interpret.
That is the same defect as a vocabulary member with no producer, and it would
put a display concern in the schema permanently.

**`text` + `CHECK`, per settled decision #100.** These are a closed set by #100's
own test, so this is a genuine departure rather than an oversight. Rejected
because the display must **average** them: `text` moves the word-to-number
mapping into every aggregate query, which is the duplication #8 forbids, to avoid
a magic number in a column no client reads directly. The `StrEnum` survives where
it earns its keep — at the boundary.

**Keep the package's `int 1..5` and rescale on load.** Multiply through and store
`2, 3, 5` or similar. Rejected because it stores a number nobody chose *and*
loses the ordinal, so every reader has to know the scaling to interpret the
column, and no round trip recovers the mentee's actual answer.

**Suppress reviews per mentor rather than per offering.** The original plan.
Rejected on implementation: per mentor it collides with the append rule — two
sessions inside a month yield one review, and the second is never even requested,
because the producer suppresses on the same predicate. A mentor strong at one
offering and weak at another is two facts.

**A settled-decision row instead of this record.** The route the plan originally
took, with the ADR sequenced last. Rejected twice over: ADR 0007 requires a new
ADR for any supersession of the canonical package, with no carve-out for one that
cannot hold its own data; and how-we-work rule 2 puts an ADR in the pull request
that *implements* it, so an ADR arriving five pull requests later would leave
`main` carrying a canonical supersession with nothing recording it.
