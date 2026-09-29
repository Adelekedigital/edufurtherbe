"""What a review is allowed to say, and when it is allowed to be written.

Two windows and one vocabulary. The interval and the vocabulary are here rather
than in ``core/config.py`` because they are **product rules, not deployment
settings** —
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

**The edit window is the exception, on request** (2026-09-29): the frontend
renders its edge from `editable_until` rather than hard-coding ten minutes, and
asked for the length to be tunable per environment. It follows the credit
ladder's shape — the size in configuration, the meaning here, and
`edit_window(settings)` as the one reader.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from app.core.config import Settings

__all__ = [
    "MENTOR_RATINGS",
    "ORDINAL_SCALE",
    "OVERALL_SCALE",
    "OVERALL_STANDS_ALONE_FROM",
    "RECOMMEND_SCALE",
    "REVIEW_INTERVAL",
    "VALUABLE_SCALE",
    "WOULD_RECOMMEND_FROM",
    "MentorRating",
    "edit_window",
    "edit_window_open",
    "editable_until",
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


#: The three-point mentor scale, as bounds. The column renders its `CHECK` from
#: this and the boundary renders its `ge`/`le` from it, so the two cannot
#: disagree about what a legal answer is — one rule, one representation.
ORDINAL_SCALE = (1, 3)

#: "How valuable was this session…", `1..5`. A genuine point scale, and the
#: figure a mentor's card showed as `X/5` until `overall_rating` arrived.
VALUABLE_SCALE = (1, 5)

#: "Your rating", step 1's stars, `1..5` (2026-09-29). Nullable in the column:
#: every review written before it shipped has none, and none is invented.
OVERALL_SCALE = (1, 5)

#: How many published overall ratings a mentor needs before their session value
#: is those alone. Below it, each review counts once — as its `overall_rating`
#: if it has one, its `valuable_rating` if not — so a mentor with a long history
#: does not swing on their first one or two star ratings. The frontend's rule,
#: confirmed by the owner 2026-09-29.
OVERALL_STANDS_ALONE_FROM = 5

#: "How likely are you to recommend…", `1..10`. **The package permits `0` and
#: the control has no zero button**, so the bound is what the form can emit.
RECOMMEND_SCALE = (1, 10)

#: The recommend score from which a mentee counts as **would recommend**, for
#: "N in 10 mentees would recommend" (#194). The owner's cut-off (2026-09-28):
#: 8 and up — stricter than 7, where nearly every review would count, and less
#: harsh than NPS's 9-and-up "promoter", which reads a mentor of mostly 8s as a
#: mentor nobody recommends.
WOULD_RECOMMEND_FROM = 8


class MentorRating(StrEnum):
    """The three answers step 1 of the form offers, in the order it offers them.

    **Declaration order is the scale**, and that is deliberate rather than
    incidental. The column stores `1`, `2` or `3`; the API publishes
    `"poor"`, `"okay"`, `"great"`. Writing the number beside the name
    would be the same mapping in two places, which non-negotiable #8 calls a
    defect — so the ordinal is the member's position and nothing declares it
    twice.

    **The hazard that creates**: reordering these members silently changes what
    every stored row means, with no migration and no failing type check.
    `test_the_scale_is_pinned_to_its_ordinals` is what makes that loud.

    **Renamed 2026-09-29** from `not_great`/`great`/`excellent` to the design's
    copy, every position kept — so no stored row changed meaning, and `great`
    now names the top of the scale where it used to name the middle.
    """

    POOR = "poor"
    OKAY = "okay"
    GREAT = "great"


#: The scale as a sequence, built once. `to_ordinal` and `from_ordinal` run four
#: times per review on every read and every write, and rebuilding the member
#: list each time is work with no reader.
_POINTS: tuple[MentorRating, ...] = tuple(MentorRating)

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
    return _POINTS.index(rating) + 1


def from_ordinal(value: int) -> MentorRating:
    """The inverse, for the read side.

    Raises ``ValueError`` on a number outside the scale rather than returning a
    default. A row holding `4` is a database that disagrees with this module,
    and answering `"great"` to it would publish a guess as a fact.
    """
    if not 1 <= value <= len(_POINTS):
        message = f"{value} is not a point on the {len(_POINTS)}-point mentor scale"
        raise ValueError(message)
    return _POINTS[value - 1]


def edit_window(settings: Settings) -> dt.timedelta:
    """How long a review stays correctable — a compose grace period, not an
    amendment window. The one reader of `review_edit_window_minutes`.

    Long enough to fix a typo and short enough that nothing has been read yet,
    which is why no revision history is kept: nothing would read it.
    """
    return dt.timedelta(minutes=settings.review_edit_window_minutes)


def editable_until(
    created_at: dt.datetime, now: dt.datetime, window: dt.timedelta
) -> dt.datetime | None:
    """When the edit window shuts, or ``None`` once it has.

    **The one statement of the edge**: `edit_window_open` is this being
    non-null, so the time a client is shown and the moment `PATCH` starts
    answering `409` cannot disagree. Half-open, matching `join_window` — at the
    edge itself the window is shut.

    **``created_at``, never ``updated_at``** — the same rule the interval uses,
    and for a sharper reason here: reading ``updated_at`` would let each edit
    restart the window, so a review could be rewritten indefinitely a few
    minutes at a time.
    """
    closes = created_at + window
    return closes if now < closes else None


def edit_window_open(created_at: dt.datetime, now: dt.datetime, window: dt.timedelta) -> bool:
    """Whether a review written at ``created_at`` may still be edited."""
    return editable_until(created_at, now, window) is not None
