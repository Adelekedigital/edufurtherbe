"""What a review is allowed to say, and when it is allowed to be written.

Two windows and one vocabulary. All three are here rather than in
``core/config.py`` because they are **product rules, not deployment settings** —
`CANCELLATION_CUTOFF` and `RESPONSE_WINDOW` in ``domain/sessions.py`` are the
same shape, and this module exists rather than joining them because reviews are
their own concern and ``domain/intake.py`` already establishes that a small
domain module is the house size.

**Why not configuration.** ``core/config.py`` holds no product rule today. A
rule in env config can differ *per environment*, so staging and production would
disagree about when a review may be written and the resulting bug is
unreproducible. The review asymmetry matters too: a constant changes by a
one-line pull request through the gate, an env var by a dashboard edit with no
test and no reviewer. If `REVIEW_INTERVAL` ever needs real runtime tuning the
precedent is a column with a server default, additive whenever it is wanted.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

__all__ = [
    "MENTOR_RATINGS",
    "ORDINAL_SCALE",
    "RECOMMEND_SCALE",
    "REVIEW_EDIT_WINDOW",
    "REVIEW_INTERVAL",
    "VALUABLE_SCALE",
    "MentorRating",
    "edit_window_open",
    "from_ordinal",
    "to_ordinal",
]

#: How long one mentee is held off reviewing the same **offering** again.
#:
#: Per offering rather than per mentor: a mentor who is excellent at CV review
#: and poor at interview prep is two facts, and suppressing the second one loses
#: signal the product exists to collect. Per *mentor* would also collide with
#: the append rule — two sessions inside a month would yield one review, and the
#: second would never be asked for at all.
REVIEW_INTERVAL = dt.timedelta(days=30)

#: A compose grace period, not an amendment window. Ten minutes is long enough
#: to fix a typo and short enough that nothing has been read yet, which is why
#: no revision history is kept: nothing would read it.
REVIEW_EDIT_WINDOW = dt.timedelta(minutes=10)

#: The three-point mentor scale, as bounds. The column renders its `CHECK` from
#: this and the boundary renders its `ge`/`le` from it, so the two cannot
#: disagree about what a legal answer is — one rule, one representation.
ORDINAL_SCALE = (1, 3)

#: "How valuable was this session…", `1..5`. A genuine point scale, and the
#: figure a mentor's card shows as `X/5`.
VALUABLE_SCALE = (1, 5)

#: "How likely are you to recommend…", `1..10`. **The package permits `0` and
#: the control has no zero button**, so the bound is what the form can emit.
RECOMMEND_SCALE = (1, 10)


class MentorRating(StrEnum):
    """The three answers step 1 of the form offers, in the order it offers them.

    **Declaration order is the scale**, and that is deliberate rather than
    incidental. The column stores `1`, `2` or `3`; the API publishes
    `"not_great"`, `"great"`, `"excellent"`. Writing the number beside the name
    would be the same mapping in two places, which non-negotiable #8 calls a
    defect — so the ordinal is the member's position and nothing declares it
    twice.

    **The hazard that creates**: reordering these members silently changes what
    every stored row means, with no migration and no failing type check.
    `test_the_scale_is_pinned_to_its_ordinals` is what makes that loud.
    """

    NOT_GREAT = "not_great"
    GREAT = "great"
    EXCELLENT = "excellent"


#: The four questions that use the scale above, in the order the form asks them.
#: Named here rather than in the model because the *boundary* iterates it too,
#: and the model's copy is a list of column names for rendering CHECKs.
MENTOR_RATINGS = (
    "communication_rating",
    "knowledge_rating",
    "practicality_rating",
    "support_rating",
)


def to_ordinal(rating: MentorRating) -> int:
    """The number the column stores, derived from position rather than declared."""
    return list(MentorRating).index(rating) + 1


def from_ordinal(value: int) -> MentorRating:
    """The inverse, for the read side.

    Raises ``ValueError`` on a number outside the scale rather than returning a
    default. A row holding `4` is a database that disagrees with this module,
    and answering `"excellent"` to it would publish a guess as a fact.
    """
    members = list(MentorRating)
    if not 1 <= value <= len(members):
        message = f"{value} is not a point on the {len(members)}-point mentor scale"
        raise ValueError(message)
    return members[value - 1]


def edit_window_open(created_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether a review written at ``created_at`` may still be edited.

    **``created_at``, never ``updated_at``** — the same rule the interval uses,
    and for a sharper reason there: reading ``updated_at`` would let each edit
    restart the window, so a review could be rewritten indefinitely a few
    minutes at a time.
    """
    return now - created_at < REVIEW_EDIT_WINDOW
