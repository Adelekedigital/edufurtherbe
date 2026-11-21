"""The one-time notices a user has dismissed (decision 356).

A notice is anything the frontend shows once and then never again: the
"what's new" tour for migrated users, an announcement, a nudge. The frontend
names each one with a key; this table remembers which keys a user dismissed
(finished or closed), so it is not shown again on any device. The backend
gives a key no meaning. The bounds are `domain.seen_items`'s; a migration test
pins the live constraint to them.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.seen_items import KEY_PATTERN
from app.infra.db.base import Base


class UserDismissedNotice(Base):
    """One notice one user dismissed, and when they first did. Insert and
    delete only: a repeat keeps the first time, so there is no `updated_at`."""

    __tablename__ = "user_dismissed_notices"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The frontend's name for the notice, e.g. ``whats-new:2026-11-migration``.
    notice_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: When the user first dismissed it; a repeat leaves it as it is.
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # One row per notice per user; it also serves every lookup by user.
        UniqueConstraint(
            "user_id", "notice_key", name="uq_user_dismissed_notices_user_id_notice_key"
        ),
        CheckConstraint(f"notice_key ~ '{KEY_PATTERN}'", name="notice_key_shape"),
    )
