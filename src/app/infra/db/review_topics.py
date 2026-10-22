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

**The service offerings a review was about** (decision 283) are the reviewed
session type's set in `session_type_offerings`, reached through the same
session. `_covers` is the one statement of that, behind all three of its
readers: each row's `service_offerings`, the `?offering=` filter and the
profile's `review_topics` counts. So a topic's count is exactly the total the
filter pages through for its slug — `ix_session_type_offerings_pair` is unique,
so a review meets each offering at most once. **Retired offerings are dropped**,
as on every mentor card: the filter refuses a retired slug, so a row or a count
naming one would offer a filter that cannot be applied.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, exists, func, literal_column, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.infra.db.models.mentoring import ServiceOffering, SessionTypeOffering
from app.infra.db.models.reviews import Review
from app.infra.db.models.sessions import Session, SessionType
from app.infra.db.offerings import live_offering

__all__ = ["about_any", "offering_counts", "offerings_column", "topic_columns", "with_topic"]


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


def _covers() -> list[Any]:
    """The reviewed session's type covers a live offering: the one predicate.

    Correlated on `Session.session_type_id`, which is null for a migrated review
    (no session) and for a session with no type, so such a review is about no
    offering.
    """
    return [
        SessionTypeOffering.session_type_id == Session.session_type_id,
        ServiceOffering.id == SessionTypeOffering.service_offering_id,
        live_offering(),
    ]


def offerings_column() -> Any:
    """Each row's offerings as a JSON array of `{slug, display_name}`, in the
    mentor's order, `[]` when it was about none. For a statement `with_topic`
    joined."""
    entry = func.json_build_object(
        literal_column("'slug'"),
        ServiceOffering.slug,
        literal_column("'display_name'"),
        ServiceOffering.display_name,
    )
    return func.coalesce(
        select(func.json_agg(aggregate_order_by(entry, SessionTypeOffering.position)))
        .select_from(SessionTypeOffering, ServiceOffering)
        .where(*_covers())
        .correlate_except(SessionTypeOffering, ServiceOffering)
        .scalar_subquery(),
        literal_column("'[]'::json"),
    ).label("service_offerings")


def about_any(slugs: Sequence[str]) -> Any:
    """`EXISTS`: the review was about at least one of the offerings `slugs`.
    For a statement `with_topic` joined."""
    return exists().where(*_covers(), ServiceOffering.slug.in_(slugs))


def offering_counts(reviews: Any) -> Select[Any]:
    """How many of the reviews `reviews` selects were about each live offering,
    most first, then by name. An offering nobody reviewed has no row.

    Joined from `reviews` through `_covers`, so the count per offering is the
    number of rows `about_any([slug])` keeps under the same `reviews`.
    """
    count = func.count().label("count")
    return (
        select(ServiceOffering.slug, ServiceOffering.display_name, count)
        .select_from(Review)
        .join(Session, Session.id == Review.session_id)
        .where(reviews, *_covers())
        .group_by(ServiceOffering.id)
        .order_by(count.desc(), ServiceOffering.display_name, ServiceOffering.slug)
    )
