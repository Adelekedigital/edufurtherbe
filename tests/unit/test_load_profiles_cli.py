"""What the loader script puts in front of the operator.

The plan computes four things nobody can act on unless they are printed:
unattached rows, ``Creator`` disagreements, award years that were nulled, and
refusals. Every one is covered by a transform test — and a transform test cannot
tell whether the value ever reaches a screen.

That gap is not hypothetical. `usage_count` was declared, indexed and documented
as the curation queue's signal, and nothing maintained it; it read as
operational and was implemented by nothing. A field the plan fills and the report
never prints is the same failure with a shorter fuse, and **`scripts/` is checked
by ruff alone** — no mypy, no bandit, and nothing counted against the coverage
floor. These are the only tests that read it.
"""

from __future__ import annotations

import pytest
from scripts.load_profiles import describe

from app.domain.transform.profiles import ProfilePlan


def plan(**overrides: object) -> ProfilePlan:
    base: dict[str, object] = {
        "education": (),
        "goals": (),
        "mentors": (),
        "awards": (),
        "errors": (),
        "unattached": {},
        "creator_mismatches": (),
        "rejected_award_years": (),
    }
    return ProfilePlan(**(base | overrides))  # type: ignore[arg-type]


def test_an_unattached_row_is_named_on_screen(capsys: pytest.CaptureFixture[str]) -> None:
    """By id, not by count. An unattached row is either legacy debris or a broken
    link, and only the id distinguishes them."""
    describe(plan(unattached={"mentor_profiles": ("1726414094626x904932306842026000",)}))

    out = capsys.readouterr().out
    assert "unattached mentor_profiles" in out
    assert "1726414094626x904932306842026000" in out


def test_a_creator_disagreement_reaches_the_operator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The path that dev data cannot exercise.

    `Creator` agrees with the user-side link on all 51 rows of the extract, so
    this reports nothing there and gets its first real workout at the production
    import — which is precisely when it needs to already work.
    """
    describe(plan(creator_mismatches=("thing-1: linked to user-a, created by user-b",)))

    assert "CREATOR MISMATCH" in capsys.readouterr().out


def test_a_nulled_award_year_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    """The award loads; the year does not. Silence here would make that
    indistinguishable from an award that never had a year."""
    describe(plan(rejected_award_years=("thing-2: 2029",)))

    out = capsys.readouterr().out
    assert "year out of range" in out
    assert "2029" in out


def test_a_refusal_is_printed_before_anything_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    describe(plan(errors=("thing-3: schoolName: required, and always kept",)))

    assert "refused" in capsys.readouterr().out


def test_a_clean_plan_says_nothing_alarming(capsys: pytest.CaptureFixture[str]) -> None:
    """The negative case. Without it every assertion above would still pass
    against a `describe` that printed all four warnings unconditionally."""
    out = capsys.readouterr()  # drain
    describe(plan())
    out = capsys.readouterr().out

    for noise in ("unattached", "CREATOR MISMATCH", "year out of range", "refused"):
        assert noise not in out
