"""Choosing "Featured this week": who may be picked, and how likely each is.

The owner's rule: automatic, weekly, a random pick weighted by **rating first,
completed sessions second, profile freshness third**, and nobody featured twice
until every eligible mentor has had a turn.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from uuid import UUID, uuid4

from app.domain.featured import Candidate, eligible, pick, week_start, weight

NOW = dt.datetime(2026, 9, 30, 12, tzinfo=dt.UTC)  # a Wednesday


def candidate(
    *, rating: float | None = None, sessions: int = 0, updated_days_ago: int | None = None
) -> Candidate:
    return Candidate(
        id=uuid4(),
        session_value=rating,
        completed_sessions=sessions,
        profile_updated_at=(
            None if updated_days_ago is None else NOW - dt.timedelta(days=updated_days_ago)
        ),
    )


# --------------------------------------------------------------------------
# The week
# --------------------------------------------------------------------------


def test_the_week_starts_on_monday_in_utc() -> None:
    assert week_start(NOW) == dt.date(2026, 9, 28)
    assert week_start(dt.datetime(2026, 9, 28, 0, 0, tzinfo=dt.UTC)) == dt.date(2026, 9, 28)
    assert week_start(dt.datetime(2026, 9, 27, 23, 59, tzinfo=dt.UTC)) == dt.date(2026, 9, 21)


def test_a_non_utc_instant_is_placed_by_its_utc_date() -> None:
    lagos = dt.timezone(dt.timedelta(hours=1))
    lagos_monday_morning = dt.datetime(2026, 9, 28, 0, 30, tzinfo=lagos)

    assert week_start(lagos_monday_morning) == dt.date(2026, 9, 21)


# --------------------------------------------------------------------------
# Weighting — the order of the three signals is the product rule
# --------------------------------------------------------------------------


def test_everyone_has_a_chance() -> None:
    """A new mentor with no rating, sessions or recent edit is still eligible:
    featuring is partly a way to be seen, and a zero weight would never be."""
    assert weight(candidate(), most_sessions=10, now=NOW) > 0


def test_rating_counts_for_more_than_sessions() -> None:
    top_rated = candidate(rating=5.0)
    busiest = candidate(sessions=10)

    assert weight(top_rated, most_sessions=10, now=NOW) > weight(busiest, most_sessions=10, now=NOW)


def test_sessions_count_for_more_than_freshness() -> None:
    busiest = candidate(sessions=10)
    freshest = candidate(updated_days_ago=0)

    assert weight(busiest, most_sessions=10, now=NOW) > weight(freshest, most_sessions=10, now=NOW)


def test_the_lowest_rating_adds_nothing() -> None:
    """A 1/5 mentor is not ahead of one nobody has reviewed yet: the bottom of
    the scale scores zero, not a fifth."""
    worst = candidate(rating=1.0)
    unreviewed = candidate()

    assert weight(worst, most_sessions=0, now=NOW) == weight(unreviewed, most_sessions=0, now=NOW)


def test_a_future_edit_is_no_fresher_than_today() -> None:
    """A clock skew or a migrated timestamp must not let freshness outrank
    sessions."""
    future = candidate(updated_days_ago=-180)
    today = candidate(updated_days_ago=0)

    assert weight(future, most_sessions=0, now=NOW) == weight(today, most_sessions=0, now=NOW)


def test_sessions_are_scaled_by_the_busiest_bookable_mentor_not_the_pool() -> None:
    """Late in a cycle the pool is small; a mentor's weight must not jump
    because the busy ones already had their turn."""
    modest = candidate(sessions=2)

    assert weight(modest, most_sessions=200, now=NOW) < weight(modest, most_sessions=2, now=NOW)


def test_a_fresher_profile_weighs_more_than_a_stale_one() -> None:
    fresh = candidate(updated_days_ago=1)
    stale = candidate(updated_days_ago=200)

    assert weight(fresh, most_sessions=0, now=NOW) > weight(stale, most_sessions=0, now=NOW)


def test_the_weighting_shows_in_what_is_picked() -> None:
    """Over many weeks the top-rated mentor comes up more often than a new one."""
    strong = candidate(rating=5.0, sessions=50, updated_days_ago=1)
    new = candidate()
    picks = Counter(
        pick([strong, new], seed=f"week-{n}", now=NOW, most_sessions=50) for n in range(400)
    )

    # About 85% by the weights; an unweighted pick would sit near half.
    assert picks[strong.id] > 0.7 * 400
    assert picks[new.id] > 0


def test_the_same_week_picks_the_same_mentor() -> None:
    pool = [candidate(rating=4.0), candidate(sessions=3), candidate()]

    first = pick(pool, seed="2026-09-28", now=NOW, most_sessions=3)

    assert first == pick(pool, seed="2026-09-28", now=NOW, most_sessions=3)


# --------------------------------------------------------------------------
# Rotation — nobody twice until everybody once
# --------------------------------------------------------------------------


def ids(n: int) -> list[UUID]:
    return [uuid4() for _ in range(n)]


def test_only_mentors_not_yet_featured_this_cycle_are_eligible() -> None:
    a, b, c = ids(3)

    pool, new_cycle = eligible({a, b, c}, featured_this_cycle={a}, last_featured=a)

    assert pool == {b, c}
    assert new_cycle is False


def test_a_finished_cycle_starts_again_without_repeating_last_week() -> None:
    a, b, c = ids(3)

    pool, new_cycle = eligible({a, b, c}, featured_this_cycle={a, b, c}, last_featured=c)

    assert pool == {a, b}
    assert new_cycle is True


def test_a_lone_mentor_can_follow_themselves() -> None:
    """With one bookable mentor, "never twice in a row" would feature nobody."""
    (a,) = ids(1)

    pool, new_cycle = eligible({a}, featured_this_cycle={a}, last_featured=a)

    assert pool == {a}
    assert new_cycle is True


def test_a_mentor_who_joined_mid_cycle_joins_this_cycle() -> None:
    a, b, newcomer = ids(3)

    pool, _ = eligible({a, b, newcomer}, featured_this_cycle={a, b}, last_featured=b)

    assert pool == {newcomer}


def test_nobody_bookable_is_nobody_eligible() -> None:
    pool, _ = eligible(set(), featured_this_cycle=set(), last_featured=None)

    assert pool == set()
