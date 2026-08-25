"""Invites, and the once-only gate one of them opens.

    referrals         somebody was invited, and how far they got
    referral_unlocks  this user has unlocked the recurring grant

**The gate is the point, not the invite.** A qualifying invite grants two
credits once — but what it really does is open `monthly_free`, which arrives
every month thereafter. That asymmetry is why `referral_unlocks` is its own
table with a uniqueness rule rather than a boolean on `users`: the question
"has this user unlocked recurring credits" is asked by the monthly job for
every mentee, and it must be one indexed lookup rather than an aggregate over
`referrals`.

**No `status` column**, where the canonical DDL declares `referral_status`
carrying `sent`, `signed_up`, `qualified`, `expired`, `rejected`. Two reasons,
and the first is the stronger:

1. Status is **entirely derivable** from `signed_up_at` and `qualified_at` —
   there is no state it can express that the timestamps cannot. Carrying both is
   one rule in two representations, which non-negotiable #8 calls a defect
   rather than a style question, and the drift it invites is a row whose status
   says `qualified` beside a null `qualified_at`.
2. Of the five values, `expired` and `rejected` have no producer. Settled
   decision #21: a vocabulary is not a wish list.

Revocation, if it ever ships, is a nullable `revoked_at` — additive, and a state
the timestamps genuinely cannot derive.

**`qualified_at` is deliberately separate from `signed_up_at`.** Inviting
somebody who signs up and vanishes must not unlock credits; without that
separation, ten throwaway addresses farm a recurring grant forever. What counts
as qualifying is signup plus a verified email today, and that is **recorded as
temporary** — the target is the invitee completing their profile, which is the
same signal as the starter credit. Tightening it is a predicate change rather
than a migration, which is exactly why the two timestamps are separate columns.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Referral(Base, TimestampMixin):
    """One invite, and how far the person it named got.

    **One row per invite, keyed by a unique code.** The code is what an arrival
    is attributed by — `invitee_email` cannot stand in, because a shared link
    carries no addressee. A code reused across rows would leave an arrival
    unable to say which invite it answered.
    """

    __tablename__ = "referrals"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    referrer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: What an invite link carries. Unique across the table — see the class
    #: docstring for why per-referrer would not identify an arrival.
    code: Mapped[str] = mapped_column(Text, nullable=False)

    #: Null for a shared link, which has no addressee. Lowercased by whoever
    #: writes it, with a CHECK that fails loudly when a writer forgets —
    #: `users.email` settled that shape, and `citext` was adopted there first
    #: and reversed.
    invitee_email: Mapped[str | None] = mapped_column(Text)

    #: Filled in when the invited person actually arrives.
    invitee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    invited_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    signed_up_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    #: **The abuse boundary.** Separate from `signed_up_at` on purpose; see the
    #: module docstring. Today: signup plus a verified email, and temporary.
    qualified_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("code", name="uq_referrals_code"),
        # Redundant against the primary key, and its only job is to be
        # referenceable: `referral_unlocks` points a composite key at
        # `(referrer_id, id)` so an unlock cannot cite an invite that
        # belongs to somebody else. Non-negotiable #10's second sentence,
        # and the third time this shape is needed in one phase.
        UniqueConstraint("referrer_id", "id", name="uq_referrals_referrer_id_id"),
        # **Scoped to the pair, and that is the rule.** On `invitee_email`
        # alone, the second referrer to invite a popular person is refused —
        # which makes a referral programme a race to invite rather than a
        # programme. Nulls are distinct in PostgreSQL, so any number of
        # link-invites coexist under this.
        UniqueConstraint("referrer_id", "invitee_email", name="uq_referrals_referrer_id_invitee"),
        CheckConstraint("invitee_email = lower(invitee_email)", name="invitee_email_is_lowercase"),
        # The cheapest possible farm is one address referring itself. The gate
        # opens a *recurring* grant, so the cost of missing this is not one
        # credit — it is three a month, indefinitely.
        CheckConstraint("referrer_id <> invitee_user_id", name="no_self_referral"),
        # Qualified without having signed up is the state the two-column split
        # exists to make impossible. `NULL` on either side leaves this unknown
        # rather than false, which is what a CHECK permits.
        CheckConstraint(
            "qualified_at IS NULL OR signed_up_at IS NOT NULL",
            name="qualified_requires_signed_up",
        ),
        # This referrer's invites, newest first — the invite list on screen.
        Index("ix_referrals_referrer_invited", "referrer_id", text("invited_at DESC")),
        # Attribution: who invited this arrival. Partial, because the column is
        # null until somebody actually shows up and most rows never fill it.
        Index(
            "ix_referrals_invitee",
            "invitee_user_id",
            postgresql_where=text("invitee_user_id IS NOT NULL"),
        ),
    )


class ReferralUnlock(Base, TimestampMixin):
    """This user has unlocked the recurring grant. At most one row per user.

    **`user_id` is not the primary key**, where the canonical DDL makes it one.
    Non-negotiable #10 admits no natural keys, no composite keys and no
    caller-supplied ids — and it says what to do instead: *"an invariant a
    natural or composite key would have carried is re-declared as `UNIQUE`"*.
    The package's own argument for the natural key — "the PK makes
    double-unlocking structurally impossible" — is preserved exactly by the
    unique constraint below. The rule was overridden twice before with every
    gate green, which is why it is now a test rather than a convention.

    **`unlocked_by_referral_id` is nullable**, where the package makes it
    `NOT NULL REFERENCES referrals(id)`. ~1,200 migrated users are grandfathered
    as unlocked and none of them ever invited anybody. The alternatives were a
    synthetic referral row per user — inventing an invite that never happened,
    in a table whose whole purpose is evidence — or letting every migrated
    mentee's balance fall to zero at the first month end, which is a migration
    that silently switches off a benefit people currently have.
    """

    __tablename__ = "referral_unlocks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: Null means grandfathered — unlocked, and the reason is absent rather than
    #: invented. See the class docstring.
    #:
    #: **No single-column foreign key.** The composite in `__table_args__` does
    #: the work: on `referrals.id` alone, user A could be unlocked citing
    #: referrer B's invite, and one invite could be cited by any number of
    #: users. Same defect the ledger's `credit_lot_id` had, same fix.
    unlocked_by_referral_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    unlocked_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # The once-only gate, structurally. Granting it twice would open a floor
        # that is already open.
        UniqueConstraint("user_id", name="uq_referral_unlocks_user_id"),
        # **The unlock and the invite that opened it belong to the same person.**
        # A single-column key on `referrals.id` is satisfied by *any* invite,
        # including one somebody else sent — and the row would then claim this
        # user was unlocked by a referral they had nothing to do with.
        #
        # `MATCH SIMPLE` is what keeps the grandfathered rows legal: they carry
        # a null `unlocked_by_referral_id`, and a composite key with any null
        # column is not checked.
        ForeignKeyConstraint(
            ["user_id", "unlocked_by_referral_id"],
            ["referrals.referrer_id", "referrals.id"],
            name="fk_referral_unlocks_unlock_belongs_to_referrer",
            ondelete="RESTRICT",
        ),
    )
