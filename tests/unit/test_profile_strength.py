"""The profile-completeness rule, as a pure function (#223)."""

from __future__ import annotations

import pytest

from app.domain.profile_strength import BOOKABILITY, COMPLETENESS_ORDER, completeness


def all_done(**overrides: bool) -> dict[str, bool]:
    return dict.fromkeys(COMPLETENESS_ORDER, True) | overrides


def test_the_order_leads_with_what_stops_bookings() -> None:
    assert COMPLETENESS_ORDER[:2] == ("session_type", "weekly_hours")
    assert set(COMPLETENESS_ORDER[:2]) == BOOKABILITY
    assert len(COMPLETENESS_ORDER) == len(set(COMPLETENESS_ORDER)) == 9


def test_missing_keeps_the_order_whatever_order_it_is_given_in() -> None:
    done = dict(reversed(list(all_done(award=False, photo=False, weekly_hours=False).items())))

    result = completeness(done)

    assert result.missing == ("weekly_hours", "photo", "award")


@pytest.mark.parametrize(("done_count", "percent"), [(0, 0), (1, 11), (5, 56), (8, 89), (9, 100)])
def test_percent_is_the_share_done_rounded(done_count: int, percent: int) -> None:
    done = {code: index < done_count for index, code in enumerate(COMPLETENESS_ORDER)}

    assert completeness(done).percent == percent


def test_an_unknown_or_missing_code_is_refused() -> None:
    """The input must name exactly the nine — a silently skipped code would
    count as neither done nor missing."""
    with pytest.raises(ValueError, match="completeness"):
        completeness(dict.fromkeys(COMPLETENESS_ORDER[:-1], True))
    with pytest.raises(ValueError, match="completeness"):
        completeness(all_done(extra=True))
