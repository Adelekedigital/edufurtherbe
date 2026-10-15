"""One permanent personal invite link per user, and its retired codes.

``invite_links`` and ``invite_link_retired_codes``.

Owner, 2026-10-15. A `referrals` row is one invite with one invitee, so its code
is single-use: the first claimant takes it and everyone after is refused. The
Mentee Home design needs one permanent link per user that anyone can sign up
through, shared in a group chat. This table holds that reusable code; each claim
through it will write its own `referrals` row for the link's owner, so every
rule a referral carries applies to each arrival unchanged.

The `a4f28c15b9e7` docstring named the cost of `UNIQUE (referrals.code)`: "a code
identifies one invite rather than a reusable link". This is the reusable link,
in its own table rather than by loosening that constraint.

**One per user**, by `UNIQUE (user_id)`, which is also what makes two concurrent
first requests agree on one code. **A code names one link**, by `UNIQUE (code)`.
The foreign key **restricts**, as every referral table's does (ADR 0013).

**No backfill**: a link is created lazily when its owner first asks for one.

**A link's code can change** (the owner picks a handle), and only the current
code is claimable. The old one goes to `invite_link_retired_codes`, which the
30-day hold is read from: `code` is not unique there, because a handle can be
retired more than once, and `(code, retired_at DESC)` serves the hold check.
Append-only, so `created_at` and no `updated_at` or trigger.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes either table until the next PR, so this lands inert.
Creating new tables locks only what they reference, briefly, to add the keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5a1e7f3d920"
down_revision: str | Sequence[str] | None = "b8f4d2e6a913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invite_links"
RETIRED = "invite_link_retired_codes"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_invite_links_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_links")),
        sa.UniqueConstraint("user_id", name=op.f("uq_invite_links_user_id")),
        sa.UniqueConstraint("code", name=op.f("uq_invite_links_code")),
    )
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.create_table(
        RETIRED,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("invite_link_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column(
            "retired_at",
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
        sa.ForeignKeyConstraint(
            ["invite_link_id"],
            ["invite_links.id"],
            name=op.f("fk_invite_link_retired_codes_invite_link_id_invite_links"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_link_retired_codes")),
    )
    op.create_index(
        "ix_invite_link_retired_codes_code_retired",
        RETIRED,
        ["code", sa.text("retired_at DESC")],
    )


def downgrade() -> None:
    """Drop both tables.

    What they held is lost: each user's personal link code, and the retired
    codes the hold is read from. A link shared before the downgrade stops
    resolving and its claim answers `404`. Arrivals already claimed through it
    keep their `referrals` rows, which never reference either table, so no
    referral, qualification or unlock is lost.
    """
    op.drop_table(RETIRED)
    op.drop_table(TABLE)
