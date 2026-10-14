"""Row-visibility predicates, defined once and imported by every store.

``LIVE`` began in ``provisioning_store.py`` as a single expression object, put
there after the same rule had been hand-typed into five statements and missed on
the fifth — the ``UPDATE``, so a user soft-deleted mid-run would have been handed
a live Supabase account.

This module exists because a second store now needs it, which is exactly the
extract-on-the-second-occurrence case non-negotiable #8 names. Every statement
that reads or writes an existing ``users`` row composes ``LIVE``; the parity test
in ``tests/unit/test_predicates.py`` walks each store's declared statements and
fails any that omits it.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import and_, true

from app.infra.db.models.user import User


def live(user: Any) -> Any:
    """``LIVE`` for an aliased ``users`` — a statement joining two people.

    One definition: ``LIVE`` below is this applied to ``User`` itself, so the
    rule cannot mean one thing for the mentor alias and another everywhere else.
    """
    return user.deleted_at.is_(None)


#: "This user still exists." An expression object rather than a string, so a
#: statement that omits it is missing a *name* — something a reader and a test
#: can both see — rather than missing a substring nobody notices.
LIVE = live(User)


class Bounds(Protocol):
    """A window's two edges: instants (`Window`) or columns of a statement,
    such as the monthly series' `VALUES` list of months (decision 247)."""

    @property
    def since(self) -> Any: ...

    @property
    def until(self) -> Any: ...


def during(column: Any, window: Bounds | None) -> Any:
    """``column`` inside a half-open ``[since, until)`` window, or no condition
    at all for ``None`` (all time).

    One statement of the boundary, so the session figures and the review
    figures of one dashboard window, and every month of the series, cannot
    disagree about which edge is in.
    """
    if window is None:
        return true()
    return and_(column >= window.since, column < window.until)
