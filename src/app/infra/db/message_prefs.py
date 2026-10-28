"""The unread message emails' switch (Messages PRD, decision 303).

One row in `notification_channel_preferences` per person who flipped it, its
`email` column the switch. **No row is on**, the PRD's default, so the read
and the job agree without a backfill. Neither function commits.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import NotificationPreferenceType
from app.infra.db.models.platform import NotificationChannelPreference

__all__ = ["set_unread_emails", "unread_emails_on"]

UNREAD = NotificationPreferenceType.UNREAD_MESSAGES


async def unread_emails_on(session: AsyncSession, user_id: UUID) -> bool:
    """Whether ``user_id`` wants the unread emails. No row means yes."""
    found = (
        await session.execute(
            select(NotificationChannelPreference.email).where(
                NotificationChannelPreference.user_id == user_id,
                NotificationChannelPreference.notification_type == UNREAD,
            )
        )
    ).scalar_one_or_none()
    return True if found is None else bool(found)


async def set_unread_emails(session: AsyncSession, user_id: UUID, on: bool) -> bool:
    """Switch the unread emails on or off for ``user_id``: one upsert, so two
    first saves both land. Returns the state saved."""
    statement = insert(NotificationChannelPreference).values(
        user_id=user_id, notification_type=UNREAD, email=on
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_notification_channel_preferences_user_type",
            set_={"email": statement.excluded.email},
        )
    )
    return on
