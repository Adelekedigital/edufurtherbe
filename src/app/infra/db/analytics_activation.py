"""The mentor activation funnel's rows (decision 338): each mentor approved in
a window, at their first approval in it, and what the database says of them.

* **cohort**: `analytics_features.approved_in`, Overview's: approvals in the
  window (read in its `tz`) of a counted mentor, so internal accounts are out.
* **hours** and **own type**: `public_visibility.weekly_hours` and
  `offerings(own=True)` standing at or after approval, so hours set while
  applying count when they were still there once approved. An offering of
  their own is any but the one onboarding created (`is_starter`).
* **booked** and **delivered**: `received_soon` and `delivered_soon`, a request
  and a completed session within `MENTOR_ACTIVATED_WITHIN` of approval.
* **calendar**: any calendar connection, a revoked one included: they connected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics_activation import ApprovedMentor
from app.domain.analytics_behaviour import Breakdown, Match
from app.domain.mentor_stats import Window
from app.infra.db.analytics_features import approved_in, delivered_soon, received_soon
from app.infra.db.models.availability import CalendarConnection
from app.infra.db.models.mentoring import MentorStatusEvent
from app.infra.db.models.user import User
from app.infra.db.public_visibility import offerings, weekly_hours

#: The person properties a database funnel can be broken down by, as columns.
PERSON_COLUMNS = {"signup_source": User.signup_source}


def person_filter(match: Match) -> Any:
    """``match`` as a condition on `User`; a property no column holds is a
    `ValueError`, so a breakdown cannot be offered that nothing reads."""
    if match.on != "person" or match.key not in PERSON_COLUMNS or match.value is None:
        raise ValueError(f"not readable from the database: {match}")
    column = PERSON_COLUMNS[match.key]
    return column != match.value if match.negate else column == match.value


def breakdown_filter(breakdown: Breakdown) -> list[Any]:
    """The people a breakdown keeps; a step's breakdown has no database form."""
    if breakdown.step is not None:
        raise ValueError("a database funnel has no step breakdown")
    return [person_filter(match) for match in breakdown.matches]


def _earliest(query: Any) -> Any:
    """The earliest `created_at` of ``query``'s rows, as a scalar that also
    reads the approval it is measured from."""
    first = query.with_only_columns(func.min(query.selected_columns[0]))
    return first.correlate(MentorStatusEvent).scalar_subquery()


async def approved_mentors(
    session: AsyncSession, window: Window, breakdown: Breakdown
) -> list[ApprovedMentor]:
    """Every mentor approved in ``window`` that ``breakdown`` keeps."""
    event = MentorStatusEvent
    approved = event.created_at
    general, dedicated = weekly_hours(since=approved)
    calendar = select(CalendarConnection.id).where(
        CalendarConnection.user_id == event.mentor_user_id
    )
    query = (
        approved_in(
            window,
            event.mentor_user_id,
            approved,
            func.least(_earliest(general), _earliest(dedicated)),
            _earliest(offerings(since=approved, own=True)),
            received_soon(event.mentor_user_id, approved),
            delivered_soon(event.mentor_user_id, approved),
            calendar.exists(),
        )
        .where(*breakdown_filter(breakdown))
        .distinct(event.mentor_user_id)
        .order_by(event.mentor_user_id, approved)
    )
    return [
        ApprovedMentor(
            user_id=row[0],
            approved_at=row[1],
            hours_at=row[2],
            own_type_at=row[3],
            booked=bool(row[4]),
            delivered=bool(row[5]),
            calendar=bool(row[6]),
        )
        for row in (await session.execute(query)).all()
    ]
