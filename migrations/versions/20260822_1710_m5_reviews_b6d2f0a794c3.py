"""The ``reviews`` table, on the scale the mentee actually chose.

**The package's declaration cannot hold the data.** ``docs/edufurther-migration``
specifies the four mentor ratings as ``int CHECK (BETWEEN 1 AND 5)``. Bubble
stores the mentee's three choices as ``1.67 / 3.34 / 5`` — ``1, 2, 3`` multiplied
by ``5/3`` for display — so an ``int`` column breaks on the first migrated row.
They land here as ``smallint CHECK (BETWEEN 1 AND 3)``, the ordinal itself, and
the transform maps ``1.67 -> 1``, ``3.34 -> 2``, ``5 -> 3``.

**The CHECK is not the loader's safety net, and this is worth being blunt
about.** ``3.34`` assigned to a ``smallint`` rounds to ``3`` and satisfies the
constraint. What the CHECK does catch is a *third* value — anything the
production export carries that the dev row did not — and it catches it loudly at
rehearsal, which is the failure mode this table wants. Dev holds one review;
production holds 53.

``nps_recommend_score`` is ``1..10`` where the package permits ``0``: the control
renders ten buttons starting at one, so nothing can produce a zero.

``public_review`` is ``NOT NULL`` where the package leaves it nullable — the form
labels it "Public review (required)". Whether all 53 legacy rows carry one is
unverified until the export lands, and the constraint is deliberately the thing
that will say so.

Additive in the only sense that matters here: a new table with no writer yet.
Nothing existing can violate it, and ``downgrade`` drops what ``upgrade`` made.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2f0a794c3"
down_revision: str | Sequence[str] | None = "a1c8e5f2b40d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reviews"

#: Written out rather than imported: **no migration imports from ``app``**, so a
#: later edit to ``SessionRole`` cannot silently rewrite what already shipped.
#: `test_every_converted_enum_has_a_check_naming_its_values` compares this
#: literal against the class, which is what keeps the two honest.
ROLES = "'mentor', 'mentee', 'observer'"


def upgrade() -> None:
    """Create the table, its eight CHECK constraints, three indexes and the trigger."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        # Nullable, and the 53 legacy reviews are why: the legacy type carries no
        # session link at all, so `NOT NULL` could not hold them. The product
        # writes one on every row.
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=False),
        sa.Column("reviewed_for", sa.Uuid(), nullable=False),
        sa.Column(
            "reviewed_for_role",
            sa.Text(),
            server_default=sa.text("'mentor'"),
            nullable=False,
        ),
        sa.Column("communication_rating", sa.SmallInteger(), nullable=False),
        sa.Column("knowledge_rating", sa.SmallInteger(), nullable=False),
        sa.Column("practicality_rating", sa.SmallInteger(), nullable=False),
        sa.Column("support_rating", sa.SmallInteger(), nullable=False),
        sa.Column("valuable_rating", sa.SmallInteger(), nullable=False),
        sa.Column("nps_recommend_score", sa.SmallInteger(), nullable=False),
        sa.Column("public_review", sa.Text(), nullable=False),
        sa.Column("private_review", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        # RESTRICT throughout: a review is evidence, and it is published on a
        # mentor's profile. ADR 0013 cascades only where the child records no
        # auditable fact of its own.
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_reviews_session_id_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_reviews_reviewed_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_for"],
            ["users.id"],
            name=op.f("fk_reviews_reviewed_for_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "communication_rating BETWEEN 1 AND 3",
            name=op.f("ck_reviews_communication_rating_range"),
        ),
        sa.CheckConstraint(
            "knowledge_rating BETWEEN 1 AND 3",
            name=op.f("ck_reviews_knowledge_rating_range"),
        ),
        sa.CheckConstraint(
            "practicality_rating BETWEEN 1 AND 3",
            name=op.f("ck_reviews_practicality_rating_range"),
        ),
        sa.CheckConstraint(
            "support_rating BETWEEN 1 AND 3",
            name=op.f("ck_reviews_support_rating_range"),
        ),
        sa.CheckConstraint(
            "valuable_rating BETWEEN 1 AND 5",
            name=op.f("ck_reviews_valuable_rating_range"),
        ),
        sa.CheckConstraint(
            "nps_recommend_score BETWEEN 1 AND 10",
            name=op.f("ck_reviews_nps_recommend_score_range"),
        ),
        sa.CheckConstraint(
            f"reviewed_for_role IN ({ROLES})",
            name=op.f("ck_reviews_reviewed_for_role_is_known"),
        ),
        sa.CheckConstraint("reviewed_by <> reviewed_for", name=op.f("ck_reviews_no_self_review")),
        sa.UniqueConstraint("legacy_bubble_id", name=op.f("uq_reviews_legacy_bubble_id")),
    )

    # One review per session per author — partial, so the rule names the rows it
    # is about instead of relying on NULLs being distinct.
    op.create_index(
        "uq_reviews_one_per_session_author",
        TABLE,
        ["session_id", "reviewed_by"],
        unique=True,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    # Shipped ahead of its reader, which is PR 3's aggregates.
    op.create_index(
        "ix_reviews_mentor_valuable",
        TABLE,
        ["reviewed_for", "valuable_rating"],
        postgresql_where=sa.text("deleted_at IS NULL AND reviewed_for_role = 'mentor'"),
    )
    # The 30-day window's index: the newest review of this mentor by this mentee.
    op.create_index(
        "ix_reviews_author_subject_created",
        TABLE,
        ["reviewed_by", "reviewed_for", sa.text("created_at DESC")],
    )

    # Per table, in the migration that creates it — `TimestampMixin` carries no
    # ORM-side `onupdate`, deliberately, because the ETL never constructs a model.
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON reviews "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """Drop the table, which takes its constraints, indexes and trigger with it."""
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON reviews")
    op.drop_index("ix_reviews_author_subject_created", table_name=TABLE)
    op.drop_index("ix_reviews_mentor_valuable", table_name=TABLE)
    op.drop_index("uq_reviews_one_per_session_author", table_name=TABLE)
    op.drop_table(TABLE)
