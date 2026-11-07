"""An admin's read-only preview of the app as one user, and its log (decision 309).

    preview_sessions  one preview, from start to its end; switching user keeps it
    preview_events    what happened in a preview, append-only

Both are audit records: kept when either person's account is purged, which
anonymises the user row and keeps its id (decision 277), so every user key
restricts. Read through SQL only for now; no admin screen reads them.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PreviewEndReason, PreviewEventKind, PreviewRevealField
from app.domain.preview import PAGE_PATH_MAX_LENGTH, REVEAL_REASON_MAX_LENGTH
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class PreviewSession(TimestampMixin, Base):
    """One admin previewing the app as one user at a time.

    **Owned by the admin and their sign-in**: `auth_session_id` is the access
    token's `session_id` claim, so a preview started in one browser is not
    usable from another, and it ends when that sign-in does. One live preview
    per sign-in (`uq_preview_sessions_live`): starting another ends this one.

    `target_since` is when the current user's preview began (the start, or the
    last switch); `revealed_at` is a reveal for that user only, so a switch
    clears it. Ended is `ended_at` and `end_reason` together, never one.
    """

    __tablename__ = "preview_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: The admin's sign-in: the access token's `session_id` claim.
    auth_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    #: When the current user's preview began: the start, or the last switch.
    target_since: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    #: The last request made in the preview; idle is measured from it.
    last_active_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    end_reason: Mapped[PreviewEndReason | None] = mapped_column(str_enum(PreviewEndReason))
    #: A reveal of the current user's messages; a switch clears it.
    revealed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(check_is_known("end_reason", PreviewEndReason), name="end_reason_is_known"),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended_with_reason"),
        CheckConstraint("admin_user_id <> target_user_id", name="not_self"),
        # One live preview per sign-in; the header's lookup is by primary key.
        Index(
            "uq_preview_sessions_live",
            "admin_user_id",
            "auth_session_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )


class PreviewEvent(Base):
    """One thing that happened in a preview, as the preview log records it.

    **Append-only** (`APPEND_ONLY` in `tests/unit/test_models.py`): `created_at`
    alone. `target_user_id` is the user being previewed at that moment, which a
    switch changes, so it is recorded here rather than read from the session.

    **Each kind carries its own detail and only it**, one `CHECK` per kind: a
    `page_view` its `path`; a `blocked_write` its `method` and `endpoint` (the
    route template, never the filled-in path); a `reveal` its `field_type` and
    `reason`; an `end` its `end_reason`. The session key restricts: a log
    outlives nothing it describes.
    """

    __tablename__ = "preview_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("preview_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[PreviewEventKind] = mapped_column(str_enum(PreviewEventKind), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    path: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(Text)
    field_type: Mapped[PreviewRevealField | None] = mapped_column(str_enum(PreviewRevealField))
    reason: Mapped[str | None] = mapped_column(Text)
    end_reason: Mapped[PreviewEndReason | None] = mapped_column(str_enum(PreviewEndReason))

    __table_args__ = (
        CheckConstraint(check_is_known("kind", PreviewEventKind), name="kind_is_known"),
        CheckConstraint(
            check_is_known("field_type", PreviewRevealField), name="field_type_is_known"
        ),
        CheckConstraint(check_is_known("end_reason", PreviewEndReason), name="end_reason_is_known"),
        CheckConstraint(
            f"char_length(path) BETWEEN 1 AND {PAGE_PATH_MAX_LENGTH}", name="path_length"
        ),
        CheckConstraint(
            f"char_length(reason) BETWEEN 1 AND {REVEAL_REASON_MAX_LENGTH}", name="reason_length"
        ),
        CheckConstraint(
            f"(kind = '{PreviewEventKind.PAGE_VIEW}') = (path IS NOT NULL)", name="page_view_detail"
        ),
        CheckConstraint(
            f"CASE WHEN kind = '{PreviewEventKind.BLOCKED_WRITE}' THEN "
            "method IS NOT NULL AND endpoint IS NOT NULL "
            "ELSE method IS NULL AND endpoint IS NULL END",
            name="blocked_write_detail",
        ),
        CheckConstraint(
            f"CASE WHEN kind = '{PreviewEventKind.REVEAL}' THEN "
            "field_type IS NOT NULL AND reason IS NOT NULL "
            "ELSE field_type IS NULL AND reason IS NULL END",
            name="reveal_detail",
        ),
        CheckConstraint(
            f"(kind = '{PreviewEventKind.END}') = (end_reason IS NOT NULL)", name="end_detail"
        ),
        # One preview's log in order.
        Index("ix_preview_events_session_created", "session_id", "created_at"),
    )
