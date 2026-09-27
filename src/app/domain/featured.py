"""Choosing "Featured this week" — the rules, with no database in sight.

The owner's rule, 2026-09-26: automatic and weekly, a **random** pick weighted by
**rating first, completed sessions second, profile freshness third**, and nobody
featured twice until every eligible mentor has had a turn — "a form of
celebrating mentors while also keeping them bookable". With twenty mentors that
is four a month and everyone within five.

**Random, and reproducible for one input.** The pick is seeded, so the same
seed over the same weights names the same mentor, which is what makes it
testable. It is **not** what keeps a week stable: freshness depends on `now`, so
two computations in one week can weigh differently. The stored row is what holds
the week — never compute the pick twice and expect agreement.

**Nobody's weight is zero.** Each signal adds to a base of one, so a mentor with
no reviews, no sessions and an old profile still has a chance. Featuring is
partly how a new mentor gets seen; a zero weight would mean never.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from app.domain.reviews import VALUABLE_SCALE

__all__ = [
    "MAX_WEEKS_AHEAD",
    "Candidate",
    "eligible",
    "pick",
    "schedule_problem",
    "week_start",
    "weight",
]

#: The order is the product rule; the gaps keep each signal outranking the next.
RATING_WEIGHT = 3.0
SESSIONS_WEIGHT = 2.0
FRESHNESS_WEIGHT = 1.0
#: A profile edited today is fully fresh; one untouched this long is not fresh.
FRESH_FOR = dt.timedelta(days=90)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A bookable mentor, with the three signals the pick weighs."""

    id: UUID
    session_value: float | None
    completed_sessions: int
    profile_updated_at: dt.datetime | None


def week_start(now: dt.datetime) -> dt.date:
    """The Monday, in UTC, of the week `now` falls in.

    UTC rather than anyone's local week: the pick is one answer for every
    viewer, and a week that began at different moments for different people
    would show two mentors at once.
    """
    day = now.astimezone(dt.UTC).date()
    return day - dt.timedelta(days=day.weekday())


def weight(candidate: Candidate, *, most_sessions: int, now: dt.datetime) -> float:
    """How likely this mentor is to be picked, relative to the others.

    Each signal is scaled to between 0 and 1 first. Sessions are logarithmic, so the
    difference between two and twenty sessions matters more than between two
    hundred and two hundred and twenty, and one very busy mentor does not
    flatten everyone else to nothing.
    """
    # Scaled from the bottom of the review scale, not from zero: a mentor rated
    # as badly as the scale allows is not ahead of one nobody has reviewed.
    low, high = VALUABLE_SCALE
    rating = (
        0.0
        if candidate.session_value is None
        else max(0.0, (candidate.session_value - low) / (high - low))
    )
    sessions = (
        math.log1p(candidate.completed_sessions) / math.log1p(most_sessions)
        if most_sessions > 0
        else 0.0
    )
    if candidate.profile_updated_at is None:
        freshness = 0.0
    else:
        age = now - candidate.profile_updated_at
        # Clamped both ways: a timestamp in the future — clock skew, or a
        # migrated `Modified Date` — is as fresh as today, never fresher.
        freshness = min(1.0, max(0.0, 1.0 - age / FRESH_FOR))
    return 1.0 + RATING_WEIGHT * rating + SESSIONS_WEIGHT * sessions + FRESHNESS_WEIGHT * freshness


def pick(
    candidates: Iterable[Candidate], *, seed: str, now: dt.datetime, most_sessions: int
) -> UUID:
    """One mentor, at random, weighted as `weight` says — the same for one seed.

    `most_sessions` is the busiest **bookable** mentor's count, not the busiest
    left in this pool. Late in a cycle the pool is small, and scaling by it would
    hand a mentor with two sessions the weight a mentor with two hundred earned
    at the cycle's start.

    Sorted by id first so the order the database returned rows in cannot change
    the answer for a given seed.
    """
    pool = sorted(candidates, key=lambda c: c.id)
    if not pool:
        raise ValueError("nobody to pick from")
    weights = [weight(c, most_sessions=most_sessions, now=now) for c in pool]
    # Not a security decision: which mentor is featured is public, and
    # predicting it gains nothing. `random` is right; `secrets` has no seed.
    chooser = random.Random(seed)  # noqa: S311  # nosec B311
    return chooser.choices([c.id for c in pool], weights=weights, k=1)[0]


def eligible(
    bookable: set[UUID],
    *,
    featured_this_cycle: set[UUID],
    last_featured: UUID | None,
) -> tuple[set[UUID], bool]:
    """Who may be picked, and whether picking starts a new cycle.

    Everyone bookable who has not had a turn this cycle. When all of them have,
    a new cycle starts with everyone again — except last week's mentor, so the
    turn of the cycle never shows the same face twice running. A mentor who
    became bookable mid-cycle is simply not yet featured, so they join it.
    """
    remaining = bookable - featured_this_cycle
    if remaining:
        return remaining, False
    fresh_cycle = bookable - {last_featured} if last_featured is not None else set(bookable)
    # One bookable mentor: "never twice running" would feature nobody at all.
    return (fresh_cycle or set(bookable)), True


#: How far ahead an admin may choose a week's mentor. Far enough to plan a
#: campaign month, near enough that a choice is still likely to be bookable
#: when its week arrives — and if it is not, the rotation fills the week.
MAX_WEEKS_AHEAD = 8


def schedule_problem(week: dt.date, *, now: dt.datetime) -> str | None:
    """Why an admin may not choose a mentor for `week`, or `None` if they may.

    A week is named by its Monday (UTC), as `week_start` gives it. The current
    week may be overridden — that is the ordinary "feature this person now" —
    and so may any of the next `MAX_WEEKS_AHEAD`. A past week is history.
    """
    current = week_start(now)
    if week.weekday() != 0:
        return "a week is named by its Monday"
    if week < current:
        return "that week has already passed"
    if week > current + dt.timedelta(weeks=MAX_WEEKS_AHEAD):
        return f"a week may be chosen at most {MAX_WEEKS_AHEAD} weeks ahead"
    return None
