"""Who wrote a review, as far as anyone but an admin may know.

**One definition for every non-admin reader** — the public list and the
subject's own list — because the rule is the same on both and two copies of it
drifted once already: the list dropped a deleted reviewer's review while the
count kept it, so a client paging the list never reached the number above it.

**A deleted reviewer's review stays; their identity goes.** The review was
earned, so its words, its value and its place in the count are the mentor's
record. The name, initial and institution are the reviewer's, and leave with
them. `author_deleted` says which case a row is, so a client labels it rather
than rendering an empty byline.

**How the identity goes is the join, not a filter applied after it.** `LIVE`
is part of the `LEFT JOIN` condition, so a deleted user's row never joins and
every column read from it is null. The institution lateral is correlated on the
*joined* user's id rather than on `reviews.reviewed_by`, so it finds nothing for
a deleted author either — correlating it on `reviewed_by` would publish the
institution of somebody who is no longer named.

Moderation reads its own join on purpose: an admin needs the author a stranger
must not see.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, and_, case, func, null, true

from app.infra.db.models.reviews import Review
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.qualifications import top_qualification

__all__ = ["author_columns", "author_session_id", "with_author"]

_QUALIFICATION = top_qualification(User.id, name="author_qualification")


def _author_gone() -> Any:
    """The joined author is not there — which, since `reviewed_by` is NOT NULL
    and restricts, means `LIVE` refused them. The one test of it."""
    return User.id.is_(None)


def author_columns() -> tuple[Any, ...]:
    """The author's public attribution, null for a deleted author."""
    return (
        User.first_name.label("author_first_name"),
        # `nullif`, because `left('', 1)` is `''` rather than null and a client
        # concatenating renders "Fauziyah .". Both name columns are nullable,
        # and the migrated rows do not go through the boundary that turns an
        # emptied string into null.
        func.nullif(func.left(User.last_name, 1), "").label("author_last_initial"),
        _QUALIFICATION.c.institution.label("author_institution"),
        # `reviewed_by` is NOT NULL and restricts, so the only way the join
        # finds no user is `LIVE` refusing one.
        _author_gone().label("author_deleted"),
    )


def author_session_id() -> Any:
    """The reviewed session, **null for a deleted author**, as `session_id`.

    The session is identity by another route: the mentor is a party to it, so
    its id leads to the reviewer the byline withholds. It goes with them, by the
    same test that empties `author_*`.
    """
    return case((_author_gone(), null()), else_=Review.session_id).label("session_id")


def with_author(statement: Select[Any]) -> Select[Any]:
    """Join the author onto a statement selecting from `reviews`."""
    return statement.outerjoin(User, and_(User.id == Review.reviewed_by, LIVE)).outerjoin(
        _QUALIFICATION, true()
    )
