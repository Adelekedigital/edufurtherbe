"""Which of the mentor's offerings a review was about.

**One definition for both lists that show it**: the public list and the
subject's own (decision 251). A second copy would drift on exactly the cases
nobody tests twice, a retired offering or a migrated review.

Reached through the session, because `reviews` holds only the session and a
second key to the offering would be the same fact twice. Both joins are outer:
the 53 migrated reviews have no session, and they stay listed with no topic. The
offering is read whatever its state now, and by its current name: retiring or
renaming it does not change what an old session was about.

**Not withheld for a deleted author**, unlike `author_session_id()`: the session
id leads to the reviewer, the offering's name does not, and the public list
shows it to everybody.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select

from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionType

__all__ = ["topic_columns", "with_topic"]


def topic_columns() -> tuple[Any, ...]:
    """`session_type_id` and `session_type_name`, null with no session."""
    return (
        SessionType.id.label("session_type_id"),
        SessionType.name.label("session_type_name"),
    )


def with_topic(statement: Select[Any]) -> Select[Any]:
    """Join the reviewed session and its offering onto a statement over `reviews`."""
    return statement.outerjoin(Session, Session.id == Review.session_id).outerjoin(
        SessionType, SessionType.id == Session.session_type_id
    )
