"""Where a bio came from: ``user_profiles.about_me_drafted_at`` and ``about_me_prompt_version``.

Mentee Onboarding v5 item #8, owner 2026-10-17 (decision 266). When the owner
saves a bio the writing assistant drafted or improved, the server stamps the
moment and stores the router's prompt version (`bio_draft-v1`); saying the text
is their own clears both. The time is never taken from a client.

**Both or neither**, by a CHECK, and **never without a bio**, by another: the
provenance describes stored text. The version's **shape** is a third CHECK:
`^[a-z][a-z0-9_]*-v[0-9]+$` and at most 64 characters, the form the router's
`PROMPT_VERSIONS` takes. Which task prefixes are known is the application's rule,
not the column's: the router adds tasks without a migration here.

**Nullable, no default, no backfill**: no bio was ever drafted. Expand only and
merged alone (project-conventions, *Migration ordering*): nothing reads or
writes the columns until the next PR. Adding nullable columns with no default
is a catalogue change; it takes a brief `ACCESS EXCLUSIVE` lock on a small table
and rewrites nothing. The CHECKs are validated over a table where both columns
are null on every row, which is instant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a6c3e9b274"
down_revision: str | Sequence[str] | None = "e4710b359d1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_profiles"
BOTH = "ck_user_profiles_about_me_provenance_both_or_neither"
SHAPE = "ck_user_profiles_about_me_prompt_version_shape"
HAS_BIO = "ck_user_profiles_about_me_provenance_has_a_bio"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column("about_me_drafted_at", sa.TIMESTAMP(timezone=True)))
    op.add_column(TABLE, sa.Column("about_me_prompt_version", sa.Text()))
    op.create_check_constraint(
        op.f(BOTH),
        TABLE,
        "(about_me_drafted_at IS NULL) = (about_me_prompt_version IS NULL)",
    )
    op.create_check_constraint(
        op.f(HAS_BIO), TABLE, "about_me_drafted_at IS NULL OR about_me IS NOT NULL"
    )
    op.create_check_constraint(
        op.f(SHAPE),
        TABLE,
        "about_me_prompt_version IS NULL OR ("
        "about_me_prompt_version ~ '^[a-z][a-z0-9_]*-v[0-9]+$' "
        "AND char_length(about_me_prompt_version) <= 64)",
    )


def downgrade() -> None:
    """Drop both columns, **refusing while any profile records a provenance**.

    Dropping them would delete the record that a bio was drafted by the
    assistant, with nobody choosing that, so a downgrade over values stops and
    says so.
    """
    op.execute("SET lock_timeout = '3s'")
    # Locked before counting, so a provenance written between the count and the
    # drop cannot be deleted without the refusal seeing it.
    op.execute(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE")
    held = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM user_profiles WHERE about_me_drafted_at IS NOT NULL")
        )
        .scalar_one()
    )
    if held:
        raise RuntimeError(
            f"{held} profile(s) record a bio's provenance; dropping the columns would delete it"
        )
    op.drop_constraint(op.f(SHAPE), TABLE)
    op.drop_constraint(op.f(HAS_BIO), TABLE)
    op.drop_constraint(op.f(BOTH), TABLE)
    op.drop_column(TABLE, "about_me_prompt_version")
    op.drop_column(TABLE, "about_me_drafted_at")
