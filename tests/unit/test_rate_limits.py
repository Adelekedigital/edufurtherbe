"""The sliding window behind the free/busy budget (decision 242)."""

from __future__ import annotations

from app.domain.rate_limits import SlidingWindow


def test_the_limit_is_allowed_and_the_next_is_refused() -> None:
    window = SlidingWindow(limit=3, window_seconds=60)

    assert [window.allow(now=t) for t in (0.0, 1.0, 2.0)] == [True, True, True]
    assert window.allow(now=10.0) is False


def test_the_window_slides_rather_than_resetting_on_a_boundary() -> None:
    """Three at 0 to 2s: a fourth at 59s is refused, at 60s it is allowed,
    because only then has the first left the last minute."""
    window = SlidingWindow(limit=3, window_seconds=60)
    for t in (0.0, 1.0, 2.0):
        window.allow(now=t)

    assert window.allow(now=59.0) is False
    assert window.allow(now=60.0) is True
    assert window.allow(now=60.5) is False


def test_a_refusal_is_not_counted() -> None:
    window = SlidingWindow(limit=1, window_seconds=60)
    window.allow(now=0.0)
    for t in range(1, 30):
        window.allow(now=float(t))

    assert window.allow(now=60.0) is True


def test_refusals_count_the_current_spell_so_it_is_reported_once() -> None:
    window = SlidingWindow(limit=1, window_seconds=60)
    window.allow(now=0.0)

    window.allow(now=1.0)
    first = window.refusals
    window.allow(now=2.0)

    assert (first, window.refusals) == (1, 2)
    window.allow(now=61.0)
    assert window.refusals == 0


def test_take_names_only_the_first_refusal_of_a_spell() -> None:
    """The one a caller logs (decisions 242, 347): never one line per request."""
    window = SlidingWindow(limit=1, window_seconds=60)

    assert window.take(now=0.0) == (True, False)
    assert window.take(now=1.0) == (False, True)
    assert window.take(now=2.0) == (False, False)
    assert window.take(now=61.0) == (True, False)
    assert window.take(now=62.0) == (False, True)
