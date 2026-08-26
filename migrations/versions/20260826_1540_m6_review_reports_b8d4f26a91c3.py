"""``review_reports`` — the subject asks, an admin decides.

A new table with no writer yet; the report endpoint and the admin queue follow.

**Nothing here removes a review.** ``reviews.deleted_at`` already exists and its
own docstring says it is *for* moderation; the profile's partial index is
``WHERE deleted_at IS NULL``, so a soft-deleted review leaves the public list
and the average without either read learning about moderation. This table adds
the reason and the record.

**A mentor cannot hide a review they dislike**, and that is the design rather
than an omission. If they could, a rating would mean nothing and a mentee
reading a five-star profile would be misled — which is what the review system
exists to prevent. The review also stays visible *while a report is pending*:
hiding on report is the same power under another name.

**The composite key decides who may report at all.** ``(review_id,
reported_by)`` against ``reviews (id, reviewed_for)`` makes "somebody else's
review" unrepresentable, rather than checked in application code that a second
writer can forget. It catches the review's own author for free — an author who
regrets a review withdraws it, and reporting is the subject's channel.

That needs ``UNIQUE (id, reviewed_for)`` on ``reviews``, redundant against its
primary key and existing only to be referenceable — non-negotiable #10's second
sentence, and the fourth time this shape has been the right answer here.

Additive: a new table, and a unique constraint on ``reviews`` that cannot fail
because ``id`` is already unique on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d4f26a91c3"
# Re-pointed when #216 merged. This stack and the monthly grant both branched
# from `f2a71d64c9e8`; the grant merged first, so this one fixes its
# `down_revision` in merge order — Alembic's chain is linear and two heads
# make `upgrade head` fail outright (how-we-work rule 1).
down_revision: str | Sequence[str] | None = "a91e37c4d820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "review_reports"

#: Written out rather than imported: **no migration imports from ``app``**.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares these
#: literals against the classes, which is what keeps the two honest.
REASONS = "'factually_inaccurate', 'abusive', 'not_this_session', 'spam'"
OUTCOMES = "'upheld', 'dismissed'"


def upgrade() -> None:
    """Make the pair referenceable, then create the table."""
    op.create_unique_constraint("uq_reviews_id_reviewed_for", "reviews", ["id", "reviewed_for"])

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("reported_by", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # Optional: `not_this_session` is checkable without prose, and demanding
        # an explanation for every report is how a queue fills with "see above".
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_reports")),
        # **Composite, and it is the authorization.** On `reviews.id` alone a
        # stranger — or the review's own author — could file against a review
        # that has nothing to do with them.
        sa.ForeignKeyConstraint(
            ["review_id", "reported_by"],
            ["reviews.id", "reviews.reviewed_for"],
            name=op.f("fk_review_reports_report_belongs_to_subject"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reported_by"],
            ["users.id"],
            name=op.f("fk_review_reports_reported_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_review_reports_resolved_by_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "review_id", "reported_by", name=op.f("uq_review_reports_review_id_reporter")
        ),
        sa.CheckConstraint(
            f"reason IN ({REASONS})", name=op.f("ck_review_reports_reason_is_known")
        ),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({OUTCOMES})",
            name=op.f("ck_review_reports_outcome_is_known"),
        ),
        # All three or none. Half-resolved is neither pending nor closed, and
        # every filter over the queue would have to guess which.
        sa.CheckConstraint(
            "num_nonnulls(resolved_at, outcome, resolved_by) IN (0, 3)",
            name=op.f("ck_review_reports_resolution_is_whole"),
        ),
    )

    # The queue: what is still waiting. Partial, because a resolved report is
    # history and the queue only ever asks for the open ones.
    op.create_index(
        "ix_review_reports_pending",
        TABLE,
        [sa.text("created_at")],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_review_reports_review", TABLE, ["review_id"])

    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table, then the constraint that existed only for it."""
    op.execute(f"DROP TRIGGER IF EXISTS trg_set_updated_at ON {TABLE}")
    op.drop_index("ix_review_reports_review", table_name=TABLE)
    op.drop_index("ix_review_reports_pending", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_constraint("uq_reviews_id_reviewed_for", "reviews", type_="unique")
