"""The facts behind the admin Analytics alert banner (PRD 31, decision 324).

Three of PRD 30's four alerts read the database and are worked out here on
every section request; `login_code_failed` reads PostHog and joins with PR 5.
The wording and thresholds are `domain.analytics`; this module only counts.

Booking requests and sign-ups leave out who the analytics leave out
(`analytics_scope`). A move is not a new request, and a migrated account is not
a sign-up. Delivery failures count every outbox row: it is the pipe's health,
whoever the message was for.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import Alert, delivery_alert, newest_first, quiet_alert
from app.infra.db.analytics_scope import counted_session, counted_user
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User


async def active_alerts(session: AsyncSession, now: dt.datetime) -> list[Alert]:
    """Every alert firing at ``now``, newest first."""
    last_request = await session.scalar(
        select(func.max(Session.created_at)).where(
            Session.rescheduled_from_session_id.is_(None), counted_session()
        )
    )
    last_signup = await session.scalar(
        select(func.max(User.created_at)).where(
            User.legacy_bubble_id.is_(None), counted_user(User.id)
        )
    )
    today = now.astimezone(dt.UTC).date()
    start = dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC)
    failed = await session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.status == "failed",
            OutboxEvent.updated_at >= start - dt.timedelta(days=1),
            OutboxEvent.updated_at < start,
        )
    )
    return newest_first(
        [
            quiet_alert("no_booking_requests", last_request, now),
            quiet_alert("no_signups", last_signup, now),
            delivery_alert(int(failed or 0), today),
        ]
    )
