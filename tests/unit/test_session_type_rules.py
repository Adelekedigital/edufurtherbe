"""The stage set's rule (#215, decision 288) and the duration range's one home (#216)."""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Table

from app.domain.availability import SESSION_DURATION_MINUTES
from app.domain.enums import ApplicationStage
from app.domain.sessions import stage_set_problem
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session, SessionTypeBookingConfig

EXPLORING, APPLYING = ApplicationStage.EXPLORING, ApplicationStage.APPLYING


def test_the_six_are_in_the_design_s_order() -> None:
    """Decision 288. Clients render them in this order."""
    assert [s.value for s in ApplicationStage] == [
        "exploring",
        "researching",
        "preparing_documents",
        "applying",
        "interview_prep",
        "awaiting_decision",
    ]


@pytest.mark.parametrize("stages", [[], [EXPLORING], [APPLYING, EXPLORING]])
def test_a_set_with_each_stage_once_is_legal(stages: list[ApplicationStage]) -> None:
    assert stage_set_problem(stages) is None


def test_a_stage_twice_names_the_field() -> None:
    assert stage_set_problem([APPLYING, APPLYING]) == (
        "/application_stages",
        "a stage appears twice",
    )


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


@pytest.mark.parametrize("schema", ["MentorSessionTypeWrite", "MentorSessionTypePatch"])
def test_the_published_venues_are_the_ones_an_offering_may_hold(schema: str) -> None:
    """Decision 289: a generated client offers `daily` and `google_meet` and no
    `custom`, from the same constant the validator refuses against."""
    from app.core.config import Settings
    from app.domain.meetings import OFFERING_VENUES
    from app.main import create_app

    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg]
    field = spec[schema]["properties"]["meeting_venue"]

    assert "meeting_venue" not in spec[schema].get("required", [])
    published = next(item for item in field["anyOf"] if item.get("type") != "null")
    assert published["enum"] == sorted(v.value for v in OFFERING_VENUES)
    assert sorted(v.value for v in OFFERING_VENUES) == ["daily", "google_meet"]
