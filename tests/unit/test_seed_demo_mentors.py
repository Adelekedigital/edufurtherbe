"""The demo seed refuses production, and builds the roster the frontend asked for."""

from __future__ import annotations

from scripts.seed_demo_mentors import refuse, roster

from app.core.config import Settings

PEOPLE = [
    {"file": f"face-{n:02d}.webp", "first_name": f"First{n}", "last_name": f"Last{n}"}
    for n in range(22)
]
OFFERINGS = ["a", "b", "c", "d", "e", "f"]


def test_production_is_refused() -> None:
    assert refuse(Settings(_env_file=None, environment="production")) is not None


def test_dev_environments_are_allowed() -> None:
    for environment in ("local", "staging"):
        assert refuse(Settings(_env_file=None, environment=environment)) is None


def test_the_roster_is_the_22_faces_and_two_without_a_photo() -> None:
    mentors = roster(PEOPLE, OFFERINGS)

    assert len(mentors) == 24
    assert sum(face is None for _, face in mentors) == 2
    assert [face for _, face in mentors[:22]] == [p["file"] for p in PEOPLE]
    assert (mentors[0][0].first_name, mentors[0][0].last_name) == ("First0", "Last0")


def test_the_roster_varies_what_the_card_shows() -> None:
    mentors = [mentor for mentor, _ in roster(PEOPLE, OFFERINGS)]

    assert all(1 <= len(m.offerings) <= 3 for m in mentors)
    assert all(set(m.offerings) <= set(OFFERINGS) for m in mentors)
    assert all(0 <= m.completed_sessions <= 60 for m in mentors)
    assert any(m.completed_sessions == 0 for m in mentors)
    assert any(not m.ratings for m in mentors) and any(m.ratings for m in mentors)
    assert sum(not m.open for m in mentors) == 2
    assert len({m.school for m in mentors}) > 5
    assert len({m.key for m in mentors}) == 24
