"""Administrative access, as grants rather than as a flag on the user row.

The legacy design put an ``Admin`` option set on ``User``. That made the grant
un-revocable in any auditable sense: clearing the field removed the access and
every trace that it had ever existed, including who gave it.

Only actual administrators have rows here. In the dev extract that is one user
out of 43.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AdminRole
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class AdminUser(TimestampMixin, Base):
    """One row per (user, role) grant, revoked in place rather than deleted.

    **All three foreign keys are ``RESTRICT``** (ADR 0012). This table is an
    audit trail, and a cascade would let a hard ``DELETE`` on ``users`` destroy
    the record of who held elevated access — partly reinstating the very defect
    the table exists to fix.

    ``granted_by`` and ``revoked_by`` restrict as well, so a user who granted
    someone administrative access cannot be hard-deleted out from under the
    grant. Whether ``SET NULL`` would be the better rule for those two is the
    open question on ADR 0012; it is theoretical while deletion means
    anonymisation, which preserves the row and its id.
    """

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    admin_role: Mapped[AdminRole] = mapped_column(pg_enum(AdminRole), nullable=False)

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
    )
