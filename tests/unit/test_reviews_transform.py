"""The scale mapping, and every way a row fails to become one.

Pure — no database, no clock, no export file. The transform is where the one
rule in this phase lives, so it is where the rule is tested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.transform.reviews import MENTOR_SCALE, plan_reviews

LAGOS = ZoneInfo("Africa/Lagos")
NEW_YORK = ZoneInfo("America/New_York")
DEV_EXPORT = Path(__file__).resolve().parents[2] / "script-data-dev" / "review.json"


def record(**overrides: Any) -> dict[str, Any]:
    """One well-formed source row, in the export's own spelling."""
    return {
        "unique id": "1755889805807x632569896287338500",
        "reviewedBy": "1734288518855x324438210652363800",
        "reviewedFor": "1734290858394x940262126235280600",
        "communicationRating": "5",
        "knowledgeRating": "3.34",
        "practicalityRating": "5",
        "supportRating": "3.34",
        "likertValuableRating": "4",
        "npsRecommendScore": "8",
        "publicReview": "He showed me what a strong portfolio looks like.",
        "privateReview": "Great platform experience",
        "Creation Date": "Aug 22, 2025 3:10 pm",
        "Modified Date": "Aug 22, 2025 3:10 pm",
    } | overrides


# --------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "ordinal"), [("1.67", 1), ("3.34", 2), ("5", 3)])
def test_every_point_on_the_scale_maps_to_its_ordinal(raw: str, ordinal: int) -> None:
    """`1 = Not great`, `2 = Great`, `3 = Excellent` — the answer the mentee
    actually chose, not Bubble's rendering of it."""
    plan = plan_reviews([record(communicationRating=raw)], timezone=LAGOS)

    assert plan.reviews[0].communication_rating == ordinal


def test_a_value_off_the_scale_is_quarantined_not_rounded() -> None:
    """**The whole reason this is a dictionary and not arithmetic.**

    `round(float("2.5") * 3 / 5)` gives `2` — a plausible-looking answer to a
    value nobody chose. And a cast is worse: settled decision #168 records that
    `3.34::smallint` rounds to `3` and *passes* `CHECK (1..3)`, so a transform
    that casts stores Excellent where the mentee chose Great, with every gate
    green. A lookup refuses what it has never seen.
    """
    plan = plan_reviews([record(communicationRating="2.5")], timezone=LAGOS)

    assert plan.reviews == ()
    assert "not a point on the three-point scale" in plan.quarantined[0].reason


def test_the_scale_has_exactly_three_points() -> None:
    """A fourth key would be somebody deciding what an unseen value means."""
    assert set(MENTOR_SCALE) == {"1.67", "3.34", "5"}
    assert sorted(MENTOR_SCALE.values()) == [1, 2, 3]


def test_the_two_point_scales_are_stored_as_chosen() -> None:
    """`valuable_rating` and `nps_recommend_score` are genuine point scales —
    nothing to unscale, so nothing to map."""
    plan = plan_reviews([record(likertValuableRating="4", npsRecommendScore="8")], timezone=LAGOS)

    assert plan.reviews[0].valuable_rating == 4
    assert plan.reviews[0].nps_recommend_score == 8


@pytest.mark.parametrize(
    ("field", "value"), [("likertValuableRating", "6"), ("npsRecommendScore", "0")]
)
def test_a_point_scale_outside_its_bounds_is_quarantined(field: str, value: str) -> None:
    """Bounded here as well as by the column, because this is where a reason can
    be written down. **`0` is the interesting one**: the package permits it and
    the control has no zero button, so a `0` in the export is a fact worth
    seeing rather than a row to squeeze in."""
    plan = plan_reviews([record(**{field: value})], timezone=LAGOS)

    assert plan.reviews == ()
    assert field in plan.quarantined[0].reason


# --------------------------------------------------------------------------
# What cannot become a row
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"reviewedBy": ""}, "reviewedBy or reviewedFor is absent"),
        ({"reviewedFor": None}, "reviewedBy or reviewedFor is absent"),
        ({"publicReview": "   "}, "publicReview is empty"),
        ({"unique id": ""}, "the row carries no unique id"),
    ],
)
def test_a_row_that_cannot_be_written_is_quarantined_with_a_reason(
    overrides: dict[str, Any], expected: str
) -> None:
    """**Reported in words, never coerced.** A quarantine line is the only form
    in which somebody can go and look at the source row."""
    plan = plan_reviews([record(**overrides)], timezone=LAGOS)

    assert plan.reviews == ()
    assert plan.quarantined[0].reason == expected


def test_a_self_review_is_refused_by_name_rather_than_by_constraint() -> None:
    """`ck_reviews_no_self_review` would refuse it anyway; saying so here names
    the field instead of the constraint."""
    same = "1734288518855x324438210652363800"
    plan = plan_reviews([record(reviewedBy=same, reviewedFor=same)], timezone=LAGOS)

    assert "the author and the subject are the same" in plan.quarantined[0].reason


def test_every_source_row_is_accounted_for() -> None:
    """The identity reconciliation checks: loaded plus quarantined equals the
    export. A row in neither vanished without anybody deciding it should."""
    plan = plan_reviews(
        [record(), record(**{"unique id": "b", "communicationRating": "9"})], timezone=LAGOS
    )

    assert plan.source_rows == 2
    assert len(plan.reviews) + len(plan.quarantined) == 2


# --------------------------------------------------------------------------
# The parts that are not derived
# --------------------------------------------------------------------------


def test_the_optional_feedback_may_be_absent() -> None:
    """`privateReview` is the only optional field on the form, and the only
    nullable text column."""
    plan = plan_reviews([record(privateReview="")], timezone=LAGOS)

    assert plan.reviews[0].private_review is None


def test_a_creator_that_disagrees_is_reported_rather_than_absorbed() -> None:
    """Settled decision #60. `reviewedBy` wins — it is the author anchor — but a
    disagreement between two source fields about who wrote a review is something
    somebody has to see."""
    plan = plan_reviews([record(Creator="somebody-else")], timezone=LAGOS)

    assert plan.reviews[0].reviewed_by == "1734288518855x324438210652363800"
    assert plan.creator_mismatches[0].ignored == "somebody-else"


def test_the_export_zone_decides_the_instant() -> None:
    """**The export carries no offset at all**, so the zone is supplied and never
    assumed — `parse_timestamp` raises rather than defaulting to UTC for exactly
    this reason. The dev application runs in `America/New_York`, so a silent
    default would shift every migrated review by hours."""
    lagos = plan_reviews([record()], timezone=LAGOS).reviews[0]
    york = plan_reviews([record()], timezone=NEW_YORK).reviews[0]

    assert lagos.created_at != york.created_at
    assert york.created_at.isoformat() == "2025-08-22T19:10:00+00:00"


def test_a_canonicalised_record_reads_the_same_as_a_raw_one() -> None:
    """**Both spellings, and the first dry run is why.**

    `JsonExportSource` canonicalises — `Creation Date` becomes `created_at` —
    so a transform reading only the export's spelling quarantines every row when
    called from the loader while passing every unit test that feeds it raw
    records. That is exactly what happened: one source row, zero reviews, and a
    reason that read like bad data.
    """
    raw = plan_reviews([record()], timezone=NEW_YORK).reviews[0]
    canonical = plan_reviews(
        [
            {
                k: v
                for k, v in record().items()
                if k not in {"unique id", "Creation Date", "Modified Date"}
            }
            | {
                "bubble_id": "1755889805807x632569896287338500",
                "created_at": "Aug 22, 2025 3:10 pm",
                "modified_at": "Aug 22, 2025 3:10 pm",
            }
        ],
        timezone=NEW_YORK,
    ).reviews[0]

    assert raw == canonical


def test_the_real_dev_export_maps_as_measured() -> None:
    """The one row that exists, through the whole transform.

    Its four ratings are `5 / 3.34 / 5 / 3.34`, which is the only real evidence
    that the scaling is what the handoff says it is — and it exercises two of the
    three points. `1.67` has no real example, which is why the parametrised test
    above carries it synthetically.
    """
    records = json.loads(DEV_EXPORT.read_text(encoding="utf-8"))

    plan = plan_reviews(records, timezone=NEW_YORK)

    assert plan.quarantined == ()
    row = plan.reviews[0]
    assert (row.communication_rating, row.knowledge_rating) == (3, 2)
    assert (row.practicality_rating, row.support_rating) == (3, 2)
    assert (row.valuable_rating, row.nps_recommend_score) == (4, 8)
