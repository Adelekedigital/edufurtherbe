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

from app.domain.analytics import Alert, delivery_alert, newest_first, quiet_alert, yesterday
from app.infra.db.analytics_features import is_sign_up
from app.infra.db.analytics_sessions import is_request
from app.infra.db.models.platform import OutboxEvent
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.predicates import during


async def active_alerts(session: AsyncSession, now: dt.datetime) -> list[Alert]:
    """Every alert firing at ``now``, newest first."""
    last_request = await session.scalar(select(func.max(Session.created_at)).where(is_request()))
    last_signup = await session.scalar(select(func.max(User.created_at)).where(is_sign_up()))
    today = now.astimezone(dt.UTC).date()
    failed = await session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.status == "failed",
            during(OutboxEvent.updated_at, yesterday(today).window),
        )
    )
    return newest_first(
        [
            quiet_alert("no_booking_requests", last_request, now),
            quiet_alert("no_signups", last_signup, now),
            delivery_alert(int(failed or 0), today),
        ]
    )
