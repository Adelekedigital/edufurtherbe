"""Admins' wording for shared emails, and its history (PRD 28, decision 318).

The defaults live in code (`domain.email_lines.LINES`); an override row replaces
one line until the line is saved back to its default, which deletes the row.
Every save is also written to `email_line_edits`, insert only. The bounds are
`domain.email_lines`'; a migration test pins the live constraints to them.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.email_lines import LINE_HARD_MAX, LINE_KEY_MAX_LENGTH
from app.domain.enums import EmailEditAction, EmailLine
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


def _fits(column: str) -> str:
    """``column`` is 1 to its line's hard max characters."""
    return " OR ".join(
        f"(line = '{line}' AND char_length({column}) BETWEEN 1 AND {most})"
        for line, most in LINE_HARD_MAX.items()
    )


_KEYS = (
    f"char_length(email_key) BETWEEN 1 AND {LINE_KEY_MAX_LENGTH} "
    f"AND char_length(variant) BETWEEN 1 AND {LINE_KEY_MAX_LENGTH}"
)


class EmailLineOverride(TimestampMixin, Base):
    """One line of one email's variant, as an admin last saved it."""

    __tablename__ = "email_line_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=sql_text("uuid_generate_v7()")
    )
    #: The registry key: a `Notification` value.
    email_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``default``, a side, or a carried case.
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[EmailLine] = mapped_column(str_enum(EmailLine), nullable=False)
    override_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The code default when it was last saved, to spot a changed default.
    default_at_edit: Mapped[str] = mapped_column(Text, nullable=False)
    #: 1 on the first save, one more on each, so two admins cannot overwrite.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    updated_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("email_key", "variant", "line"),
        CheckConstraint(check_is_known("line", EmailLine), name="line_is_known"),
        CheckConstraint(_fits("override_text"), name="override_text_length"),
        CheckConstraint(_fits("default_at_edit"), name="default_at_edit_length"),
        CheckConstraint(_KEYS, name="key_length"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_email_line_overrides_updated_by", "updated_by"),
    )


class EmailLineEdit(Base):
    """One changed line in one save: the email edit history. Insert only."""

    __tablename__ = "email_line_edits"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=sql_text("uuid_generate_v7()")
    )
    email_key: Mapped[str] = mapped_column(Text, nullable=False)
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[EmailLine] = mapped_column(str_enum(EmailLine), nullable=False)
    #: What was sent before and after (an override or the default), and the
    #: code default at that moment.
    before_text: Mapped[str] = mapped_column(Text, nullable=False)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    default_text: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[EmailEditAction] = mapped_column(str_enum(EmailEditAction), nullable=False)
    #: The admin who saved it. Restricts, as `preview_events` does: a history
    #: outlives nothing it names (the account purge anonymises, never deletes).
    admin_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint(check_is_known("line", EmailLine), name="line_is_known"),
        CheckConstraint(check_is_known("action", EmailEditAction), name="action_is_known"),
        CheckConstraint(_fits("before_text"), name="before_text_length"),
        CheckConstraint(_fits("after_text"), name="after_text_length"),
        CheckConstraint(_fits("default_text"), name="default_text_length"),
        CheckConstraint(_KEYS, name="key_length"),
        # The list's "Last edited": the newest row per email.
        Index("ix_email_line_edits_email_key_created_at", "email_key", sql_text("created_at DESC")),
        Index("ix_email_line_edits_admin_id", "admin_id"),
    )
