"""Turning 53 legacy reviews into rows, and the scaling that cannot be cast away.

**The whole reason this module exists is one mapping.** Bubble stores a
three-point scale as ``1.67 / 3.34 / 5`` — the ordinals ``1, 2, 3`` multiplied by
``5/3`` so they render on a five-point display — and the column stores the
ordinal the mentee actually chose.

**A cast would look like it worked.** ``3.34`` assigned to a ``smallint`` rounds
to ``3`` and satisfies ``CHECK (BETWEEN 1 AND 3)``, so a transform that casts
stores *Excellent* where the mentee chose *Great*: every row loaded, every gate
green, and nothing anywhere saying the data is wrong. That is settled decision
#168, and ``test_a_scaled_legacy_value_rounds_instead_of_failing`` is its
executable form. The mapping here is a dictionary for exactly that reason —
lookup fails loudly on a value nobody predicted, where arithmetic succeeds
quietly on all of them.

**Keyed on the string, not on a float.** The export renders every value as text
(``'3.34'``), and ``float('3.34') * 3 / 5`` is not ``2.0`` — comparing parsed
floats would reintroduce the guesswork the dictionary exists to remove.

WHAT IS NOT DERIVED, AND WHY
============================
**No ``session_id``.** The legacy ``Reviews`` type carries no link to a session,
confirmed three ways. ``03_MIGRATION_RUNBOOK.md`` says to match ``reviewedBy`` +
``reviewedFor`` + proximity to a completed session; that has no anchor — 8 of 12
dev mentee/mentor pairs have more than one booking — so every migrated row takes
``session_id = NULL`` and the link stays absent rather than invented. A later
backfill is additive; a fabricated link is not.

**``reviewed_for_role`` is always ``mentor``.** The legacy table is one
direction: a mentee's account of a mentor. Note that ``02_FIELD_MAPPING.md`` §6
calls the column ``reviewed_role``; the table names it ``reviewed_for_role``
because ``reviewed_for`` names a *user* and this names the capacity they were
reviewed in.

**``Creator`` is not a fallback.** It equals ``reviewedBy`` in the one dev row,
and ``reviewedBy`` is the author anchor regardless — but a disagreement is
reported rather than absorbed, following settled decision #60.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.domain.bubble import blank_to_none, legacy_anchor, parse_timestamp
from app.domain.reviews import MENTOR_RATINGS

__all__ = [
    "MENTOR_SCALE",
    "QuarantinedReview",
    "ReviewPlan",
    "ReviewRow",
    "plan_reviews",
]

#: Bubble's rendering of the three-point scale, and the ordinal behind each.
#:
#: **A lookup, not arithmetic.** `round(float(raw) * 3 / 5)` produces the same
#: three answers and also produces an answer for `2.15`, which the product can
#: neither emit nor interpret. A dictionary refuses what it has never seen, which
#: is the behaviour the 53 unseen production rows need.
MENTOR_SCALE: dict[str, int] = {"1.67": 1, "3.34": 2, "5": 3}

#: The two point scales that need no mapping — they are stored as chosen.
POINT_SCALES = ("likertValuableRating", "npsRecommendScore")

#: Legacy field names, in the order the table's columns appear.
SOURCE_RATINGS = (
    "communicationRating",
    "knowledgeRating",
    "practicalityRating",
    "supportRating",
)


def _text(value: Any) -> str | None:
    """A trimmed value, or ``None`` for anything the export renders as empty.

    `blank_to_none` is the shared rule — the export writes `""` where the API
    omits the field — and this narrows its `Any` to the string this module
    actually wants, so a caller cannot forget which of the two it is holding.
    """
    cleaned = blank_to_none(value)
    return str(cleaned).strip() if cleaned is not None else None


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """One review, ready to write."""

    legacy_bubble_id: str
    reviewed_by: str
    reviewed_for: str
    communication_rating: int
    knowledge_rating: int
    practicality_rating: int
    support_rating: int
    valuable_rating: int
    nps_recommend_score: int
    public_review: str
    private_review: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class QuarantinedReview:
    """A source row that cannot become one, and the reason in words.

    **Reported, never loaded, never coerced.** A rating outside the known scale
    is a fact about the export that somebody has to look at — guessing the
    nearest ordinal would publish that guess as a mentee's answer.
    """

    legacy_bubble_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Disagreement:
    """Two source fields stating different things, and which one won."""

    legacy_bubble_id: str
    field: str
    kept: str
    ignored: str


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """Everything one extract turns into, before any of it is written."""

    reviews: tuple[ReviewRow, ...]

    #: Source rows that cannot be written. Reported so the count adds up.
    quarantined: tuple[QuarantinedReview, ...] = ()

    #: `Creator` disagreeing with `reviewedBy`. The link wins; this exists so the
    #: disagreement is visible rather than absorbed (settled decision #60). One
    #: dev row agrees, which is not evidence about 53.
    creator_mismatches: tuple[Disagreement, ...] = ()

    @property
    def source_rows(self) -> int:
        """Every row the export offered, however it was accounted for."""
        return len(self.reviews) + len(self.quarantined)

    @property
    def ok(self) -> bool:
        """Whether the load may proceed at all.

        **A quarantine is not an error**, which is the distinction the other
        transforms draw too: a row nobody can attribute is a fact about the
        export, and refusing the whole load over it would hold 52 good reviews
        hostage to one bad one. What makes a run *not clean* is reported
        separately, and `load_reviews.py` exits non-zero for it.
        """
        return True

    def report(self) -> str:
        """The operator-facing account of one run.

        Lives in `domain` beside the decisions it summarises, per settled
        decision #45 — these counts *are* the result, and a store that formats
        text for a terminal has stopped being a store.

        **The quarantine block is the deliverable, not a footnote.** Each line is
        a review that did not migrate and the reason in words, which is the only
        form in which somebody can go and look at the source row.
        """
        lines = [
            f"source rows: {self.source_rows}",
            f"reviews to load: {len(self.reviews)}",
            f"quarantined: {len(self.quarantined)}",
            f"Creator disagreed with reviewedBy: {len(self.creator_mismatches)}",
        ]
        lines += [f"  QUARANTINED {row.legacy_bubble_id}: {row.reason}" for row in self.quarantined]
        lines += [
            f"  CREATOR {row.legacy_bubble_id}: kept {row.kept}, ignored {row.ignored}"
            for row in self.creator_mismatches
        ]
        return "\n".join(lines)


def _ordinal(raw: str | None, *, field: str) -> tuple[int | None, str | None]:
    """The stored ordinal for a scaled value, or the reason it has none."""
    if raw is None:
        return None, f"{field} is absent"
    ordinal = MENTOR_SCALE.get(raw)
    if ordinal is None:
        return None, f"{field} is {raw!r}, which is not a point on the three-point scale"
    return ordinal, None


def _point(raw: str | None, *, field: str, high: int) -> tuple[int | None, str | None]:
    """A genuine point scale, parsed and bounded.

    Bounded here as well as by the column, because the transform is where a
    reason can be written down. The `CHECK` would refuse the row with a message
    about a constraint; this refuses it with a message about a field.
    """
    if raw is None:
        return None, f"{field} is absent"
    try:
        value = int(raw)
    except ValueError:
        return None, f"{field} is {raw!r}, which is not a whole number"
    if not 1 <= value <= high:
        return None, f"{field} is {value}, outside 1..{high}"
    return value, None


def _either(record: dict[str, Any], canonical: str, raw: str) -> Any:
    """A field under whichever name this record happens to carry.

    **Both names, for the reason `legacy_anchor` takes both.** A record read
    through `JsonExportSource` has been canonicalised — `Creation Date` becomes
    `created_at` — while one read straight from the file has not. Reading only
    the canonical name makes this transform work from the loader and silently
    quarantine everything when called with raw records, which is exactly how the
    first dry run of this module reported one source row and zero reviews.
    """
    value = record.get(canonical)
    return value if value is not None else record.get(raw)


def _stamp(value: Any, *, timezone: dt.tzinfo) -> dt.datetime | None:
    """One export timestamp, or ``None`` if it is absent or unreadable.

    **The zone is supplied rather than assumed**, because the export renders
    local times with no offset at all — `parse_timestamp` raises rather than
    defaulting to UTC for exactly that reason, and the dev application runs in
    `America/New_York`, so a silent default would shift every migrated review by
    hours.
    """
    raw = _text(value)
    if raw is None:
        return None
    try:
        return parse_timestamp(raw, assume=timezone)
    except ValueError:
        return None


def plan_reviews(records: list[dict[str, Any]], *, timezone: dt.tzinfo) -> ReviewPlan:
    """Every review the export offers, as rows or as reasons it has none.

    Pure: no I/O, no database, no clock. The timezone is a parameter because the
    export renders local times with no offset, and which zone that is belongs to
    the Bubble application rather than to this function.
    """
    rows: list[ReviewRow] = []
    quarantined: list[QuarantinedReview] = []
    mismatches: list[Disagreement] = []

    for record in records:
        anchor = legacy_anchor(record)
        author = _text(record.get("reviewedBy"))
        subject = _text(record.get("reviewedFor"))
        public = _text(record.get("publicReview"))

        if not anchor:
            quarantined.append(QuarantinedReview("<no id>", "the row carries no unique id"))
            continue
        if not author or not subject:
            quarantined.append(QuarantinedReview(anchor, "reviewedBy or reviewedFor is absent"))
            continue
        if author == subject:
            # `ck_reviews_no_self_review` would refuse it anyway; saying so here
            # names the field rather than the constraint.
            quarantined.append(QuarantinedReview(anchor, "the author and the subject are the same"))
            continue
        if not public:
            quarantined.append(QuarantinedReview(anchor, "publicReview is empty"))
            continue

        values: dict[str, int] = {}
        reason: str | None = None
        for source, column in zip(SOURCE_RATINGS, MENTOR_RATINGS, strict=True):
            ordinal, reason = _ordinal(_text(record.get(source)), field=source)
            if ordinal is None:
                break
            values[column] = ordinal
        if reason is not None:
            quarantined.append(QuarantinedReview(anchor, reason))
            continue

        valuable, reason = _point(
            _text(record.get("likertValuableRating")), field="likertValuableRating", high=5
        )
        if valuable is None:
            quarantined.append(QuarantinedReview(anchor, reason or "likertValuableRating"))
            continue
        nps, reason = _point(
            _text(record.get("npsRecommendScore")), field="npsRecommendScore", high=10
        )
        if nps is None:
            quarantined.append(QuarantinedReview(anchor, reason or "npsRecommendScore"))
            continue

        creator = _text(record.get("Creator"))
        if creator is not None and creator != author:
            mismatches.append(Disagreement(anchor, "Creator", kept=author, ignored=creator))

        created = _stamp(_either(record, "created_at", "Creation Date"), timezone=timezone)
        if created is None:
            quarantined.append(QuarantinedReview(anchor, "Creation Date is absent or unparseable"))
            continue
        rows.append(
            ReviewRow(
                legacy_bubble_id=anchor,
                reviewed_by=author,
                reviewed_for=subject,
                valuable_rating=valuable,
                nps_recommend_score=nps,
                public_review=public,
                private_review=_text(record.get("privateReview")),
                created_at=created,
                updated_at=_stamp(
                    _either(record, "modified_at", "Modified Date"), timezone=timezone
                )
                or created,
                **values,
            )
        )

    return ReviewPlan(
        reviews=tuple(rows),
        quarantined=tuple(quarantined),
        creator_mismatches=tuple(mismatches),
    )
