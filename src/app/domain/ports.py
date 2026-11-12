"""What the product needs from outside, named without naming who provides it.

A Protocol here states the need; an adapter in `infra` satisfies it and a
factory there chooses the real one or the null one from settings (decision
239). Synchronous, like every adapter: async callers reach them through
`asyncio.to_thread`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from app.core.reporting import ErrorContext
from app.domain.analytics_behaviour import BehaviourFigures, ExploreFigures, Funnel, LoginCodes
from app.domain.mentor_stats import Window

__all__ = ["AnalyticsReader", "AnalyticsSink", "ErrorReporter"]


class ErrorReporter(Protocol):
    """Where an unhandled request error is reported besides Sentry (decision 339)."""

    def report(self, exc: BaseException, context: ErrorContext) -> None:
        """Report ``exc`` with ``context``. **Never raises and never waits on
        the vendor**: the error handler calls it before answering the 500."""
        ...


class AnalyticsSink(Protocol):
    """Where product analytics events go (PRD 30)."""

    def capture(self, events: list[dict[str, Any]]) -> None:
        """Send these events, built by `analytics_event`, in one request.
        Raises `UpstreamError` when they were not taken."""
        ...

    def delete_person(self, user_id: UUID) -> None:
        """Erase the person ``user_id`` names, and their events, at the vendor
        (the account purge). Raises `UpstreamError` when it was not taken."""
        ...


class AnalyticsReader(Protocol):
    """Where product-analytics figures are read from (PRD 31, decision 331).

    Every method raises `AnalyticsUpstreamError` when the source
    cannot answer, whatever the cause; nothing else escapes. ``internal`` is
    the internal accounts' `users.id`s, which the vendor knows as their
    distinct ids: none of them is counted.
    """

    def funnel(
        self, funnel: Funnel, by: str, window: Window, internal: Sequence[UUID]
    ) -> list[int]:
        """People reaching each of the funnel's tracked steps, in order, for
        those entering it in ``window`` and narrowed by the ``by`` option."""
        ...

    def behaviour(self, window: Window, internal: Sequence[UUID]) -> BehaviourFigures:
        """The event cards' counts in ``window``."""
        ...

    def explore(self, windows: Sequence[Window], internal: Sequence[UUID]) -> ExploreFigures:
        """Searchers per window, and the first window's who booked soon after."""
        ...

    def login_codes(self, window: Window, internal: Sequence[UUID]) -> LoginCodes:
        """Log-in code attempts and failures in ``window``."""
        ...

    def profile_links_shared(
        self, cohorts: Sequence[Sequence[tuple[UUID, dt.datetime]]], internal: Sequence[UUID]
    ) -> list[int]:
        """Per cohort, how many of its mentors (each with their approval)
        shared their own profile link within `MENTOR_ACTIVATED_WITHIN` of it.
        One failure fails the whole read."""
        ...

    def known_persons(self, user_ids: Sequence[UUID], internal: Sequence[UUID]) -> set[UUID]:
        """Which of ``user_ids`` the vendor already has events for (decision
        340): the one-off person sync updates only those, never creating one."""
        ...
