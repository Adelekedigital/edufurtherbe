"""The stage-set label rule (#212) and the duration range's one home (#213)."""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Table

from app.domain.availability import SESSION_DURATION_MINUTES
from app.domain.enums import ApplicationStage
from app.domain.sessions import stage_label_problem
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session, SessionTypeBookingConfig

DRAFTING, REVISIONS, OTHER = (
    ApplicationStage.DRAFTING_STAGE,
    ApplicationStage.REVISIONS,
    ApplicationStage.OTHER,
)


@pytest.mark.parametrize(
    ("stages", "label"),
    [
        ([], None),
        ([DRAFTING], None),
        ([DRAFTING, REVISIONS], None),
        ([OTHER], "Gap year"),
        # `other` behind a named stage — what the old symmetric CHECK refused.
        ([DRAFTING, OTHER], "Gap year"),
    ],
)
def test_a_legal_set_and_label(stages: list[ApplicationStage], label: str | None) -> None:
    assert stage_label_problem(stages, label) is None


@pytest.mark.parametrize(
    ("stages", "label", "pointer"),
    [
        ([DRAFTING, OTHER], None, "/custom_stage_label"),
        ([OTHER], None, "/custom_stage_label"),
        ([DRAFTING], "stray", "/custom_stage_label"),
        ([], "stray", "/custom_stage_label"),
        ([DRAFTING, DRAFTING], None, "/application_stages"),
    ],
)
def test_an_illegal_set_and_label_names_the_field(
    stages: list[ApplicationStage], label: str | None, pointer: str
) -> None:
    problem = stage_label_problem(stages, label)

    assert problem is not None
    assert problem[0] == pointer


def _check(table: Table, name: str) -> str:
    (constraint,) = [
        c
        for c in table.constraints
        if isinstance(c, CheckConstraint) and str(c.name).endswith(name)
    ]
    return str(constraint.sqltext)


@pytest.mark.parametrize(
    ("table", "name"),
    [
        (SessionTypeBookingConfig.__table__, "duration_minutes_valid"),
        (Session.__table__, "duration_minutes_valid"),
        (MentorProfile.__table__, "default_duration_minutes_valid"),
    ],
)
def test_every_duration_check_is_the_one_range(table: Table, name: str) -> None:
    """The write models read `SESSION_DURATION_MINUTES`; each column restates
    it as a `CHECK`. Pinned, so the two cannot drift (#8)."""
    low, high = SESSION_DURATION_MINUTES

    assert f"BETWEEN {low} AND {high}" in _check(table, name)
