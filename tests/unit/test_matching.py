"""The ranking, the reasons and `exact` for mentor matching (decision 265)."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from app.domain.matching import (
    DEGREE_ABOVE,
    DEGREE_EXACT,
    Mentee,
    Mentor,
    Ref,
    best_field,
    field_score,
    is_exact,
    rank,
    reasons,
    score,
)
from app.domain.search import FUZZY_FLOOR, fold

HELP_A, HELP_B = uuid4(), uuid4()
NIGERIA, GHANA = Ref(uuid4(), "Nigeria"), Ref(uuid4(), "Ghana")
UK, CANADA, US = Ref(uuid4(), "United Kingdom"), Ref(uuid4(), "Canada"), Ref(uuid4(), "US")
MASTERS = Ref(uuid4(), "Master's")
MASTERS_RANK, DOCTORATE_RANK, BACHELORS_RANK = 7, 8, 6

MENTEE = Mentee(
    goal_offering_ids=frozenset({HELP_A, HELP_B}),
    field="computer science",
    origin=NIGERIA,
    countries=(UK, CANADA),
    degree_rank=MASTERS_RANK,
)


def mentor(**facts: object) -> Mentor:
    """A mentor matching nothing; each knob turns on one signal."""
    base = Mentor(profile_id=uuid4(), shuffle="8" * 32)
    return replace(base, **facts)  # type: ignore[arg-type]


#: One mentor per key, each scoring on that key alone, highest key first.
ONE_KEY_EACH = [
    {"offering_ids": (HELP_A,)},
    {"field_similarity": 0.9, "field_text": "Computer Science"},
    {"origin": NIGERIA},
    {"has_open_slot": True},
    {"study_country": UK},
    {"level": MASTERS, "level_rank": MASTERS_RANK},
]

#: Every key below the one named, all at once.
EVERYTHING_BELOW = {
    n: {k: v for lower in ONE_KEY_EACH[n + 1 :] for k, v in lower.items()}
    for n in range(len(ONE_KEY_EACH))
}


@pytest.mark.parametrize("key", range(len(ONE_KEY_EACH) - 1))
def test_each_key_beats_every_key_below_it_together(key: int) -> None:
    higher = mentor(**ONE_KEY_EACH[key])
    lower = mentor(**EVERYTHING_BELOW[key])

    assert rank(MENTEE, [lower, higher]) == [higher, lower]
    assert rank(MENTEE, [higher, lower]) == [higher, lower]


def test_the_lowest_key_still_counts() -> None:
    degree = mentor(**ONE_KEY_EACH[-1])
    nothing = mentor()

    assert rank(MENTEE, [nothing, degree]) == [degree, nothing]


def test_more_shared_help_areas_rank_higher() -> None:
    two = mentor(offering_ids=(HELP_A, HELP_B))
    one = mentor(offering_ids=(HELP_A,), **EVERYTHING_BELOW[0])

    assert rank(MENTEE, [one, two]) == [two, one]


def test_a_closer_field_ranks_higher_once_both_match() -> None:
    close = mentor(field_similarity=0.9, field_text="Computer Science")
    fair = mentor(field_similarity=0.5, field_text="Computer Engineering", **EVERYTHING_BELOW[1])

    assert rank(MENTEE, [fair, close]) == [close, fair]


def test_a_field_below_the_floor_scores_nothing() -> None:
    assert field_score(FUZZY_FLOOR - 0.01) == 0
    assert field_score(FUZZY_FLOOR) == FUZZY_FLOOR
    assert field_score(None) == 0


def test_any_open_slot_counts_the_same() -> None:
    a, b = mentor(has_open_slot=True), mentor(has_open_slot=True)

    assert score(MENTEE, a) == score(MENTEE, b)


def test_a_first_choice_country_beats_a_second() -> None:
    first = mentor(study_country=UK)
    second = mentor(study_country=CANADA, level=MASTERS, level_rank=MASTERS_RANK)

    assert score(MENTEE, first)[4] == 1.0
    assert score(MENTEE, second)[4] == 0.5
    assert rank(MENTEE, [second, first]) == [first, second]
    assert score(MENTEE, mentor(study_country=US))[4] == 0


@pytest.mark.parametrize(
    ("rank_", "expected"),
    [(MASTERS_RANK, DEGREE_EXACT), (DOCTORATE_RANK, DEGREE_ABOVE), (BACHELORS_RANK, 0.0)],
)
def test_degree_is_exact_one_higher_half_lower_nothing(rank_: int, expected: float) -> None:
    assert score(MENTEE, mentor(level=MASTERS, level_rank=rank_))[5] == expected


def test_a_missing_fact_on_either_side_scores_zero() -> None:
    nothing_known = Mentee()
    full = mentor(**{k: v for each in ONE_KEY_EACH for k, v in each.items()})

    assert score(nothing_known, full) == (0, 0, 0, 1, 0, 0)
    assert score(MENTEE, mentor()) == (0, 0, 0, 0, 0, 0)
    assert reasons(nothing_known, full) == []


def test_ties_follow_the_shuffle_then_the_newest_profile() -> None:
    # The id order alone would put `late` first, so only the shuffle orders them.
    early = mentor(shuffle="1" * 32, profile_id=UUID(int=10))
    late = mentor(shuffle="2" * 32, profile_id=UUID(int=20))
    older = mentor(shuffle="5" * 32, profile_id=UUID(int=1))
    newer = mentor(shuffle="5" * 32, profile_id=UUID(int=2))

    assert rank(MENTEE, [late, early]) == [early, late]
    assert rank(MENTEE, [older, newer]) == [newer, older]


def test_every_reason_in_ranking_order() -> None:
    full = mentor(
        offering_ids=(HELP_B, uuid4(), HELP_A),
        field_similarity=0.9,
        field_text="Computer Science",
        origin=NIGERIA,
        has_open_slot=True,
        study_country=CANADA,
        level=MASTERS,
        level_rank=DOCTORATE_RANK,
    )

    found = reasons(MENTEE, full)

    assert [r.code for r in found] == ["help_areas", "field", "origin", "study_country", "degree"]
    assert found[0].offering_ids == (HELP_B, HELP_A)
    assert found[1].field == "Computer Science"
    assert found[2].ref == NIGERIA
    assert found[3].ref == CANADA
    assert found[4].ref == MASTERS


def test_no_reason_for_a_signal_that_did_not_match() -> None:
    unmatched = mentor(
        offering_ids=(uuid4(),),
        field_similarity=FUZZY_FLOOR - 0.1,
        field_text="History",
        origin=GHANA,
        has_open_slot=True,
        study_country=US,
        level=MASTERS,
        level_rank=BACHELORS_RANK,
    )

    assert reasons(MENTEE, unmatched) == []


def test_exact_needs_every_answered_primary_signal() -> None:
    all_three = mentor(
        offering_ids=(HELP_A,), field_similarity=0.9, field_text="CS", origin=NIGERIA
    )

    assert is_exact(MENTEE, all_three)
    for missing in ({"offering_ids": ()}, {"field_similarity": 0.1}, {"origin": GHANA}):
        assert not is_exact(MENTEE, replace(all_three, **missing))  # type: ignore[arg-type]


def test_exact_skips_a_signal_the_mentee_did_not_answer() -> None:
    no_origin = replace(MENTEE, origin=None)
    top = mentor(offering_ids=(HELP_A,), field_similarity=0.9, field_text="CS")

    assert is_exact(no_origin, top)


def test_not_exact_with_nothing_to_match_on_or_nobody() -> None:
    assert not is_exact(Mentee(), mentor(has_open_slot=True))
    assert not is_exact(MENTEE, None)


def test_fold_ignores_case_and_accents() -> None:
    assert fold("  Économie   du Développement ") == fold("economie du developpement")
    assert fold("Économie") == "economie"
    assert fold("!!!") == ""


def test_the_best_scoring_field_text_wins_and_unscored_texts_are_skipped() -> None:
    scored = [("History", 0.1), ("Physics", None), ("Computer Science", 0.9), ("CS", 0.9)]

    assert best_field(scored) == (0.9, "Computer Science")
    assert best_field([("Physics", None)]) is None
    assert best_field([]) is None
