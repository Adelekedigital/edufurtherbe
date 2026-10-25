"""Why a party missed a session, in their own words (Session Join PRD).

    session_missed_reasons  one reason per party per missed session

Its own module rather than `sessions.py`, which already holds six models and is
past #54's tripwire. The subject is still the session: a reason means nothing
without the session it explains.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import MissedReason
from app.domain.missed_reasons import NOTE_MAX_LENGTH
from app.infra.db.base import Base
from app.infra.db.types import check_is_known, str_enum


class SessionMissedReason(Base):
    """One party's reason for missing one session. **Internal**: admins read it,
    the other party never does.

    **Append-only** (`APPEND_ONLY` in `tests/unit/test_models.py`): a reason is
    what somebody said at a moment, so `created_at` alone, and a second send is
    refused by the unique pair rather than overwriting the first.

    The session key **cascades**, as `session_participants` does: a reason
    means nothing without its session, and sessions are retained, so nothing
    reaches this table through a user deletion. The user key **restricts**, as
    the participant's does. The note is free text the person wrote, so the
    account purge clears it (decision 277).
    """

    __tablename__ = "session_missed_reasons"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    #: The party who missed it, always the caller who sent it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[MissedReason] = mapped_column(str_enum(MissedReason), nullable=False)
    #: Optional, trimmed at the boundary, blank stored as null.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # One reason per party per session: the second send is a `409`, caught
        # by this name (#169). It also serves the admin read by `session_id`.
        UniqueConstraint("session_id", "user_id", name="uq_session_missed_reasons_session_user"),
        CheckConstraint(check_is_known("reason", MissedReason), name="reason_is_known"),
        CheckConstraint(f"char_length(note) <= {NOTE_MAX_LENGTH}", name="note_length"),
    )
