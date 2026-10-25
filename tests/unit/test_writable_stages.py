"""The design's six application stages, and how a retired stage reads
(Session Types PRD, founder 2026-10-21)."""

from __future__ import annotations

import pytest

from app.domain.enums import ApplicationStage
from app.domain.sessions import RETIRED_STAGES, WritableStage, current_stages

A = ApplicationStage
W = WritableStage


def test_the_six_are_in_the_design_s_order() -> None:
    assert [w.value for w in W] == [
        "exploring",
        "researching",
        "preparing_documents",
        "applying",
        "interview_prep",
        "awaiting_decision",
    ]


def test_every_writable_stage_is_a_stored_one() -> None:
    """Pins the second vocabulary to the column's (non-negotiable 8): a stage a
    write may name is one the `CHECK` accepts."""
    assert {w.value for w in W} <= {a.value for a in A}


def test_every_stored_stage_is_either_current_or_retired() -> None:
    """A member of `ApplicationStage` that is neither would read as a crash."""
    current = {w.value for w in W}
    for stage in A:
        assert (stage.value in current) != (stage in RETIRED_STAGES), stage


@pytest.mark.parametrize(
    ("retired", "becomes"),
    [
        (A.EARLY_EXPLORATION, W.EXPLORING),
        (A.DRAFTING_STAGE, W.PREPARING_DOCUMENTS),
        (A.REVISIONS, W.PREPARING_DOCUMENTS),
        (A.POST_SUBMISSION, W.AWAITING_DECISION),
        (A.INTERVIEWING, W.INTERVIEW_PREP),
        (A.OTHER, None),
    ],
)
def test_the_founder_s_remap(retired: ApplicationStage, becomes: WritableStage | None) -> None:
    assert RETIRED_STAGES[retired] is becomes


def test_a_stored_set_reads_as_the_six_in_its_order() -> None:
    stored = [A.REVISIONS, A.OTHER, A.EARLY_EXPLORATION, A.DRAFTING_STAGE, A.APPLYING]

    assert current_stages(stored) == [W.PREPARING_DOCUMENTS, W.EXPLORING, W.APPLYING]


def test_a_current_set_reads_as_itself() -> None:
    stored = [A.AWAITING_DECISION, A.RESEARCHING]

    assert current_stages(stored) == [W.AWAITING_DECISION, W.RESEARCHING]


def test_a_set_of_only_other_reads_as_any_stage() -> None:
    assert current_stages([A.OTHER]) == []
