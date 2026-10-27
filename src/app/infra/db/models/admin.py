"""Administrative access, as grants rather than as a flag on the user row.

The legacy design put an ``Admin`` option set on ``User``. That made the grant
un-revocable in any auditable sense: clearing the field removed the access and
every trace that it had ever existed, including who gave it.

Only actual administrators have rows here. In the dev extract that is one user
out of 43. The notes admins leave on a mentor for each other live here too.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.admin_notes import NOTE_BODY_MAX_LENGTH
from app.domain.enums import AdminRole
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class AdminUser(TimestampMixin, Base):
    """One row per (user, role) grant, revoked in place rather than deleted.

    **All three foreign keys are ``RESTRICT``** (ADR 0013). This table is an
    audit trail, and a cascade would let a hard ``DELETE`` on ``users`` destroy
    the record of who held elevated access — partly reinstating the very defect
    the table exists to fix.

    ``granted_by`` and ``revoked_by`` restrict as well, so a user who granted
    someone administrative access cannot be hard-deleted out from under the
    grant. Whether ``SET NULL`` would be the better rule for those two is the
    open question on ADR 0013; it is theoretical while deletion means
    anonymisation, which preserves the row and its id.
    """

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    admin_role: Mapped[AdminRole] = mapped_column(str_enum(AdminRole), nullable=False)

    # Nullable, and null is the honest value for every migrated row: the legacy
    # option set recorded that someone was an admin, never who made them one. A
    # synthetic "system" actor would look like knowledge we do not have.
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        # One live grant per (user, role); revoked rows stay and do not block a
        # re-grant. Without the partial clause, revoking and later re-granting
        # the same role would collide with the historical row — which is how an
        # audit trail becomes something people delete rows from to get work done.
        Index(
            "ix_admin_users_active_grant",
            "user_id",
            "admin_role",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # `revoked_by` without `revoked_at` is a row that names a revoker while
        # still matching `WHERE revoked_at IS NULL` — i.e. it counts as an active
        # grant in the index above while the audit trail reads "revoked by X".
        # For a table whose entire purpose is being auditable, the two must move
        # together. Bare name: the `ck` convention renders the prefix.
        CheckConstraint(
            "revoked_by IS NULL OR revoked_at IS NOT NULL",
            name="revoker_implies_revocation",
        ),
        # Settled decision #100: the vocabulary is guarded here, not by a
        # PostgreSQL type. `ix_admin_users_active_grant` indexes this column but
        # names no value in its predicate, so it rebuilt with the table.
        CheckConstraint(
            check_is_known("admin_role", AdminRole),
            name="admin_role_is_known",
        ),
    )


class MentorAdminNote(Base):
    """An internal note one admin left on a mentor, for the other admins.

    **Only admins read these**; the mentor never does (Admin PRD, 2026-10-26).
    **Append-only** (`APPEND_ONLY` in `tests/unit/test_models.py`): a note is
    what somebody wrote at a moment, so `created_at` alone and no edit.

    The mentor key **cascades** from `mentor_profiles`, as
    `mentor_guide_countries` does: a note means nothing without the profile it
    is about; a hard delete of a mentor would take them, which is intended, and
    no path makes one (deletion soft-deletes the profile and anonymises the
    user). The author key **restricts**, as this module's grants do, so the
    record of who wrote a note cannot be deleted out from under it; deletion
    anonymises the user row and keeps its id (decision 277).
    """

    __tablename__ = "mentor_admin_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentor_profiles.user_id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # The drawer reads one mentor's notes oldest first.
        Index("ix_mentor_admin_notes_mentor", "mentor_user_id", "created_at"),
        CheckConstraint(
            f"char_length(body) BETWEEN 1 AND {NOTE_BODY_MAX_LENGTH}", name="body_length"
        ),
    )
