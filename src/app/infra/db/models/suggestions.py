"""A time a mentor suggested when declining or cancelling (#339, decision 230).

**Its own table, not columns on ``sessions``.** A suggestion is not a session:
the original ends as it would have, and the suggestion is an *offer* that may
become a new session or may lapse. Columns on the original would give a
declined row a future ``starts_at`` it never had, and every reader of
``sessions`` would have to learn to ignore it.

**Deletion policy (ADR 0013):** ``session_id`` cascades — the offer means
nothing without the session it was made about — and every other key restricts,
as the rest of the sessions schema does.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class SessionSuggestion(TimestampMixin, Base):
    """One suggested time, held for one mentee until ``held_until``.

    **Active** means ``accepted_session_id IS NULL AND held_until > now`` —
    written once, in ``infra/db/holds.py``, and read by the slot grid, the
    booking writer and the session read alike. There is no status column and no
    sweep: a hold that lapses simply stops counting, so nothing has to run for it
    to be released.

    ``mentor_id``, ``mentee_id``, ``session_type_id`` and ``duration_minutes``
    are copied from the original rather than joined to it, because the hold is
    read on the hot path of every slot request and a join to ``sessions`` there
    would be one more table on the query every mentee's calendar makes.
    """

    __tablename__ = "session_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    #: The session that was declined or cancelled. One suggestion per session.
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    mentor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mentee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    session_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("session_types.id", ondelete="RESTRICT"), nullable=False
    )
    starts_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    held_until: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    #: The session the mentee booked from it. Set once, in the booking's
    #: transaction; non-null means the offer is spent.
    accepted_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        Index("uq_session_suggestions_session", "session_id", unique=True),
        # The hold lookup: a mentor's suggestions that may still hold a slot.
        Index(
            "ix_session_suggestions_open_holds",
            "mentor_id",
            "held_until",
            postgresql_where=text("accepted_session_id IS NULL"),
        ),
        CheckConstraint("duration_minutes > 0", name="duration_is_positive"),
        CheckConstraint("mentor_id <> mentee_id", name="parties_differ"),
    )
