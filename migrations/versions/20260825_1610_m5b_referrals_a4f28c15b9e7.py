"""``referrals`` and ``referral_unlocks`` — the invite, and the gate it opens.

Two new tables with no writer yet; PR 5 brings the endpoints.

**No ``referral_status``.** The canonical DDL declares the type carrying
``sent``, ``signed_up``, ``qualified``, ``expired``, ``rejected`` and a column
holding it *beside* ``signed_up_at`` and ``qualified_at``. Status is entirely
derivable from those two timestamps, so carrying both is one rule in two
representations — non-negotiable #8 — and the drift it invites is a row reading
``qualified`` beside a null ``qualified_at``. Two of the five values have no
producer besides (#21). No type is created here, so none has to be dropped
later.

**``referral_unlocks`` gets a surrogate id**, where the package makes
``user_id`` the primary key. ADR 0015 and non-negotiable #10 admit no natural
keys; the invariant the natural key carried — one unlock per user, ever — is
re-declared as ``UNIQUE (user_id)``.

**``unlocked_by_referral_id`` is nullable**, where the package makes it
``NOT NULL``. Migrated users are unlocked without a referral, and the
alternative is a synthetic invite per user in a table whose purpose is evidence.

**``UNIQUE (code)``, where the package indexes it non-uniquely.** Its
``idx_referrals_code`` implies a code per *referrer*; a per-referrer code cannot
tell an arrival which invite it answered, and ``invitee_email`` cannot stand in
because a shared link has no addressee. The cost is that a code identifies one
invite rather than a reusable link — recorded because it is a real divergence,
not an obvious one.

**``UNIQUE (referrer_id, invitee_email)`` is the package's own rule**, at
``05_credits_reviews.sql:137``, and is carried unchanged. Noted because an
earlier draft of this migration claimed it as a departure; it is not.

**Foreign keys ``RESTRICT``**, where the package says ``CASCADE`` (ADR 0013).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4f28c15b9e7"
down_revision: str | Sequence[str] | None = "e7c1a9d4b350"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFERRALS = "referrals"
UNLOCKS = "referral_unlocks"


def upgrade() -> None:
    """Create both tables, their three CHECKs, four uniques, two indexes and two triggers."""
    op.create_table(
        REFERRALS,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("referrer_id", sa.Uuid(), nullable=False),
        # What an invite link carries, and what an arrival is attributed by.
        sa.Column("code", sa.Text(), nullable=False),
        # Null for a shared link, which has no addressee. `text`, not `citext` —
        # `users.email` adopted that type first and reversed it, because a
        # case-insensitive type is a second mechanism for an invariant the
        # boundary already holds.
        sa.Column("invitee_email", sa.Text(), nullable=True),
        sa.Column("invitee_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "invited_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("signed_up_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Deliberately separate from `signed_up_at`: that separation is the
        # abuse boundary. Somebody who signs up and vanishes unlocks nothing.
        sa.Column("qualified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referrals")),
        sa.ForeignKeyConstraint(
            ["referrer_id"],
            ["users.id"],
            name=op.f("fk_referrals_referrer_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invitee_user_id"],
            ["users.id"],
            name=op.f("fk_referrals_invitee_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("code", name=op.f("uq_referrals_code")),
        # Redundant against the primary key on purpose: it exists so
        # `referral_unlocks` can point a composite key at `(referrer_id, id)`.
        sa.UniqueConstraint("referrer_id", "id", name=op.f("uq_referrals_referrer_id_id")),
        # Scoped to the pair. On `invitee_email` alone the second referrer to
        # invite a popular person is refused, which makes the programme a race.
        sa.UniqueConstraint(
            "referrer_id", "invitee_email", name=op.f("uq_referrals_referrer_id_invitee")
        ),
        sa.CheckConstraint(
            "invitee_email = lower(invitee_email)",
            name=op.f("ck_referrals_invitee_email_is_lowercase"),
        ),
        sa.CheckConstraint(
            "referrer_id <> invitee_user_id", name=op.f("ck_referrals_no_self_referral")
        ),
        sa.CheckConstraint(
            "qualified_at IS NULL OR signed_up_at IS NOT NULL",
            name=op.f("ck_referrals_qualified_requires_signed_up"),
        ),
    )

    op.create_index(
        "ix_referrals_referrer_invited", REFERRALS, ["referrer_id", sa.text("invited_at DESC")]
    )
    # Partial: the column is null until somebody arrives, and most rows never
    # fill it.
    op.create_index(
        "ix_referrals_invitee",
        REFERRALS,
        ["invitee_user_id"],
        postgresql_where=sa.text("invitee_user_id IS NOT NULL"),
    )

    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {REFERRALS} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        UNLOCKS,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # Nullable, where the package says NOT NULL. Migrated users are unlocked
        # without a referral; the alternative invents an invite that never
        # happened.
        sa.Column("unlocked_by_referral_id", sa.Uuid(), nullable=True),
        sa.Column(
            "unlocked_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referral_unlocks")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_referral_unlocks_user_id_users"),
            ondelete="RESTRICT",
        ),
        # **Composite.** On `referrals.id` alone, user A could be unlocked
        # citing referrer B's invite, and one invite could be cited by any
        # number of users. `MATCH SIMPLE` keeps the grandfathered rows legal,
        # because they carry a null referral.
        sa.ForeignKeyConstraint(
            ["user_id", "unlocked_by_referral_id"],
            ["referrals.referrer_id", "referrals.id"],
            name=op.f("fk_referral_unlocks_unlock_belongs_to_referrer"),
            ondelete="RESTRICT",
        ),
        # The once-only gate, carrying the invariant the package's natural
        # primary key would have carried.
        sa.UniqueConstraint("user_id", name=op.f("uq_referral_unlocks_user_id")),
    )

    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {UNLOCKS} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop both tables, the dependent one first."""
    op.execute(f"DROP TRIGGER IF EXISTS trg_set_updated_at ON {UNLOCKS}")
    op.drop_table(UNLOCKS)

    op.execute(f"DROP TRIGGER IF EXISTS trg_set_updated_at ON {REFERRALS}")
    op.drop_index("ix_referrals_invitee", table_name=REFERRALS)
    op.drop_index("ix_referrals_referrer_invited", table_name=REFERRALS)
    op.drop_table(REFERRALS)
