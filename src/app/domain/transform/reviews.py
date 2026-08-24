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
from app.domain.reviews import RECOMMEND_SCALE, VALUABLE_SCALE
from app.domain.transform.sessions import Disagreement

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

#: The two genuine point scales, with the field each is bounded by. **Named
#: rather than retyped at the point of use**, and the bound comes from
#: `domain/reviews.py` — the same constants the model renders its CHECKs from, so
#: the transform and the column cannot disagree about what is legal (#8).
POINT_COLUMNS: dict[str, tuple[str, tuple[int, int]]] = {
    "likertValuableRating": ("valuable_rating", VALUABLE_SCALE),
    "npsRecommendScore": ("nps_recommend_score", RECOMMEND_SCALE),
}

#: Legacy field name to column, **paired by name rather than by position**.
#:
#: The first version zipped this against `MENTOR_RATINGS`, whose own docstring
#: calls it *"the four questions… in the order the form asks them"* — a **UI**
#: ordering, with nothing anywhere saying the ETL depends on it. Reordering it
#: for a form change would silently swap two ratings on every migrated review:
#: `strict=True` still passes, all four CHECKs still pass, and the single dev row
#: (`3 / 2 / 3 / 2`) cannot see the two likeliest swaps.
RATING_COLUMNS: dict[str, str] = {
    "communicationRating": "communication_rating",
    "knowledgeRating": "knowledge_rating",
    "practicalityRating": "practicality_rating",
    "supportRating": "support_rating",
}


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
class ReviewPlan:
    """Everything one extract turns into, before any of it is written."""

    reviews: tuple[ReviewRow, ...]

    #: Every anchor the export offered, in order. **Reconciliation needs a
    #: source list to be missing from**: comparing the plan against itself
    #: agrees with itself and proves nothing, which is the defect
    #: `reconcile.py` records shipping once before — *"every test passed,
    #: because none of them gave the plan a source list to be missing from"*.
    source_anchors: tuple[str, ...] = ()

    #: Source rows that cannot be written. Reported so the count adds up.
    quarantined: tuple[QuarantinedReview, ...] = ()

    #: `Creator` disagreeing with `reviewedBy`. The link wins; this exists so the
    #: disagreement is visible rather than absorbed (settled decision #60). One
    #: dev row agrees, which is not evidence about 53.
    creator_mismatches: tuple[Disagreement, ...] = ()

    @property
    def source_rows(self) -> int:
        """Every row the export offered, however it was accounted for."""
        return len(self.source_anchors)

    #: **There is deliberately no `ok`.** The sibling transforms carry one
    #: because they have failure modes distinct from a quarantine — an
    #: overlapping window aborts the load outright. This one does not: every way
    #: a source row can fail is a quarantine, and a quarantine is not a reason to
    #: refuse 52 good reviews. A property returning a literal `True` would read
    #: like a gate and be none.

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
        lines += [f"  CREATOR {row}" for row in self.creator_mismatches]
        return "\n".join(lines)


def _ordinal(raw: str | None, *, field: str) -> tuple[int | None, str | None]:
    """The stored ordinal for a scaled value, or the reason it has none."""
    if raw is None:
        return None, f"{field} is absent"
    ordinal = MENTOR_SCALE.get(raw)
    if ordinal is None:
        return None, f"{field} is {raw!r}, which is not a point on the three-point scale"
    return ordinal, None


def _point(
    raw: str | None, *, field: str, bounds: tuple[int, int]
) -> tuple[int | None, str | None]:
    """A genuine point scale, parsed and bounded.

    Bounded here as well as by the column, because the transform is where a
    reason can be written down. The `CHECK` would refuse the row with a message
    about a constraint; this refuses it with a message about a field.

    **The bound is the same constant the column renders its CHECK from.** Typing
    `1..10` here would be the rule in two places, and the register already asks
    whether any of the 53 rows carries NPS `0` — relaxing that bound would move
    the column and leave this quarantining every row it just made legal (#8).
    """
    low, high = bounds
    if raw is None:
        return None, f"{field} is absent"
    try:
        value = int(raw)
    except ValueError:
        return None, f"{field} is {raw!r}, which is not a whole number"
    if not low <= value <= high:
        return None, f"{field} is {value}, outside {low}..{high}"
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


def _stamp(value: Any, *, timezone: dt.tzinfo) -> tuple[dt.datetime | None, str | None]:
    """One export timestamp, and which of the two ways it can be missing.

    **Absent and unreadable are different facts.** Collapsing them let an
    unparseable `Modified Date` silently become `created_at`, which is a value
    Bubble never sent — and reconciliation cannot catch it, because it compares
    the table against the plan that invented it.

    **The zone is supplied rather than assumed**, because the export renders
    local times with no offset at all — `parse_timestamp` raises rather than
    defaulting to UTC for exactly that reason, and the dev application runs in
    `America/New_York`, so a silent default would shift every migrated review by
    hours.
    """
    raw = _text(value)
    if raw is None:
        return None, "is absent"
    try:
        return parse_timestamp(raw, assume=timezone), None
    except ValueError:
        return None, f"is {raw!r}, which is not a timestamp this export writes"


def plan_reviews(records: list[dict[str, Any]], *, timezone: dt.tzinfo) -> ReviewPlan:
    """Every review the export offers, as rows or as reasons it has none.

    Pure: no I/O, no database, no clock. The timezone is a parameter because the
    export renders local times with no offset, and which zone that is belongs to
    the Bubble application rather than to this function.
    """
    rows: list[ReviewRow] = []
    quarantined: list[QuarantinedReview] = []
    mismatches: list[Disagreement] = []
    seen: set[str] = set()

    source_anchors: list[str] = []

    for record in records:
        anchor = legacy_anchor(record)
        source_anchors.append(anchor or "<no id>")
        author = _text(record.get("reviewedBy"))
        subject = _text(record.get("reviewedFor"))
        public = _text(record.get("publicReview"))

        if not anchor:
            quarantined.append(QuarantinedReview("<no id>", "the row carries no unique id"))
            continue
        if anchor in seen:
            # **Otherwise it disappears twice over.** The upsert collapses a
            # repeated anchor into one row and `reconcile_reviews` keys `expected`
            # on the same value, so a review the operator was told would load
            # silently does not, with every check green.
            quarantined.append(QuarantinedReview(anchor, "the export repeats this unique id"))
            continue
        seen.add(anchor)
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
        for source, column in RATING_COLUMNS.items():
            ordinal, reason = _ordinal(_text(record.get(source)), field=source)
            if ordinal is None:
                break
            values[column] = ordinal
        if reason is not None:
            quarantined.append(QuarantinedReview(anchor, reason))
            continue

        for source, (column, bounds) in POINT_COLUMNS.items():
            point, reason = _point(_text(record.get(source)), field=source, bounds=bounds)
            if point is None:
                break
            values[column] = point
        if reason is not None:
            quarantined.append(QuarantinedReview(anchor, reason))
            continue

        created, reason = _stamp(_either(record, "created_at", "Creation Date"), timezone=timezone)
        if created is None:
            quarantined.append(QuarantinedReview(anchor, f"Creation Date {reason}"))
            continue
        modified, reason = _stamp(
            _either(record, "modified_at", "Modified Date"), timezone=timezone
        )
        if modified is None and reason != "is absent":
            # **Absent may fall back; unreadable may not.** A row Bubble never
            # stamped can honestly take its creation time. A row Bubble stamped
            # with something this cannot read is a fact about the export, and
            # substituting `created_at` fabricates a value it never sent — which
            # reconciliation then confirms, because it compares the table against
            # this same plan.
            quarantined.append(QuarantinedReview(anchor, f"Modified Date {reason}"))
            continue
        # **Recorded only once the row is certain to exist.** Noting it earlier
        # counted a disagreement on a row that was then quarantined, so two
        # identical rows were accounted for differently — and a mismatch forces
        # a non-zero exit, so it also changed the run's verdict.
        creator = _text(record.get("Creator"))
        if creator is not None and creator != author:
            mismatches.append(
                Disagreement(anchor, f"Creator is {creator}, kept reviewedBy {author}")
            )

        rows.append(
            ReviewRow(
                legacy_bubble_id=anchor,
                reviewed_by=author,
                reviewed_for=subject,
                public_review=public,
                private_review=_text(record.get("privateReview")),
                created_at=created,
                updated_at=modified or created,
                **values,
            )
        )

    return ReviewPlan(
        reviews=tuple(rows),
        source_anchors=tuple(source_anchors),
        quarantined=tuple(quarantined),
        creator_mismatches=tuple(mismatches),
    )
