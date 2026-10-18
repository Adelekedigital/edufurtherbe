"""The writing assistant's own bookkeeping (Onboarding v5 B7, decision 266).

    writing_assist_requests  one call to the AI router, for the daily limit

Its own module because the subject is the writing assistant, not the profile it
helps with (#33): nothing here describes a person or their bio.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import WritingAssistField
from app.infra.db.base import Base
from app.infra.db.types import check_is_known, str_enum


class WritingAssistRequest(Base):
    """One call the writing assistant made for a user, counted by the daily limit.

    **A row is written only when a call is actually made**, so a request the
    limit refused, or one made while the feature is off, costs nothing. The
    limit counts a user's rows for one field over the last 24 hours on the
    database clock, which the index below serves.

    **Its own table, where decision 235 counts existing rows**: an intake upload
    leaves an `intake_files` row to count, while writing assist saves nothing,
    so there is no other row that records a call was made.

    **Append-only** (`APPEND_ONLY` in `tests/unit/test_models.py`): a call is a
    fact about a moment, so `created_at` alone. Holds no text: what was sent and
    what came back are never stored.

    **Cascades with its user.** The rows mean nothing without the person and
    record no evidence anybody needs afterwards.
    """

    __tablename__ = "writing_assist_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[WritingAssistField] = mapped_column(str_enum(WritingAssistField), nullable=False)
    #: When the call was made; the limit's window is measured from it.
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(check_is_known("field", WritingAssistField), name="field_is_known"),
        # The limit: this user's calls for this field since a moment.
        Index(
            "ix_writing_assist_requests_user_field_created",
            "user_id",
            "field",
            text("created_at DESC"),
        ),
    )
