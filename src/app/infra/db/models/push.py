"""A browser's Web Push subscription, one row per device (#386).

`endpoint` is unique across every user: one browser profile has one endpoint,
so a second account signing in there takes the row over. The bounds are
`domain.push`'s; a migration test pins the live constraints to them.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.push import (
    AUTH_MAX_LENGTH,
    ENDPOINT_MAX_LENGTH,
    ENDPOINT_PATTERN,
    P256DH_MAX_LENGTH,
)
from app.infra.db.base import Base, TimestampMixin


class PushSubscription(TimestampMixin, Base):
    """Where to push to one of a user's browsers, and the keys to encrypt to."""

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The push service's URL for this browser profile.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: The browser's P-256 public key, base64url (RFC 8291).
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    #: The browser's authentication secret, base64url (RFC 8291).
    auth: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"endpoint ~ '{ENDPOINT_PATTERN}' AND octet_length(endpoint) <= {ENDPOINT_MAX_LENGTH}",
            name="endpoint_shape",
        ),
        CheckConstraint(
            f"char_length(p256dh) BETWEEN 1 AND {P256DH_MAX_LENGTH}", name="p256dh_length"
        ),
        CheckConstraint(f"char_length(auth) BETWEEN 1 AND {AUTH_MAX_LENGTH}", name="auth_length"),
        # A recipient's devices, for every push sent to them.
        Index("ix_push_subscriptions_user_id", "user_id"),
    )
