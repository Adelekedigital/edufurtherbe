"""M2 degree levels aligned to ISCED

Six seeded rows become four, because the original set mixed three different
kinds of thing.

WHAT WAS WRONG
==============
    Undergraduate   an education *level*
    Diploma         a level, below bachelor's
    Masters         a level
    MBA             not a level — a *specific* master's degree
    PhD             a level
    Postdoctoral    not a qualification at all — a temporary research **post**
                    held by somebody who already has a doctorate. Nothing is
                    awarded, so nobody "has" it as a degree.

A filter over that list cannot answer "mentors with a doctorate", because a
holder might be on `phd` or on `postdoc`, and an MBA holder is on `mba` rather
than with the other master's degrees.

THE NEW SET, AND WHERE IT COMES FROM
====================================
UNESCO's **ISCED 2011** — the International Standard Classification of
Education, which exists to make qualifications comparable *across countries*.
That is this platform's problem exactly: a Nigerian BSc, a UK BA and a US
Bachelor's are the same level and three different words.

    slug         display_name             ISCED
    diploma      Certificate / Diploma    4-5
    bachelors    Bachelor's Degree        6
    masters      Master's Degree          7
    doctorate    Doctorate (PhD)          8

**No `isced_level` column.** Nothing would read it, and a column maintained by
nothing is the defect this schema already removed once — `usage_count` was
declared, indexed and documented, and was zero on every row forever. The mapping
lives here; the column arrives when a feature needs it.

THE REMAP
=========
    undergraduate -> bachelors      renamed
    phd           -> doctorate      renamed
    mba           -> masters        merged; an MBA *is* a master's degree
    postdoc       -> doctorate      merged; a postdoc holder holds a doctorate

Rows pointing at `mba` and `postdoc` are repointed before those rows are
deleted, so no `education_entries.degree_level_id` or `mentee_goals.degree_goal_id`
is orphaned. Nothing is lost either way:
``education_entries.degree_category`` keeps the raw legacy string, so a
regretted merge is recoverable from what the source actually said.

Safe now precisely because it is now: the only data in flight is the dev
extract, which is test data. After the production import this would be a
backfill over 940 real rows.

Revision ID: b83f0e51d7a2
Revises: a71c4d3e8b96
Create Date: 2026-08-09 15:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b83f0e51d7a2"
down_revision: str | Sequence[str] | None = "a71c4d3e8b96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (from_slug, into_slug) — merged away, and where their rows go.
MERGES = (("mba", "masters"), ("postdoc", "doctorate"))

#: (old_slug, new_slug, display_name, sort_order) — ascending by ISCED level, so
#: the dropdown reads as a progression rather than alphabetically.
RENAMES = (
    ("diploma", "diploma", "Certificate / Diploma", 10),
    ("undergraduate", "bachelors", "Bachelor's Degree", 20),
    ("masters", "masters", "Master's Degree", 30),
    ("phd", "doctorate", "Doctorate (PhD)", 40),
)


def upgrade() -> None:
    """Upgrade schema.

    **Bound parameters, not string interpolation.** The first version built these
    by `%`-formatting and suppressed ruff's `S608` to do it — and "Bachelor's
    Degree" broke the statement on its apostrophe, which is exactly the class of
    bug that rule exists to prevent. A suppressed warning was right about the
    very line it was suppressed on.
    """
    rename = sa.text(
        "UPDATE degree_levels SET slug = :new_slug, display_name = :display_name, "
        "sort_order = :sort_order WHERE slug = :old_slug"
    )
    for old_slug, new_slug, display_name, sort_order in RENAMES:
        op.get_bind().execute(
            rename,
            {
                "old_slug": old_slug,
                "new_slug": new_slug,
                "display_name": display_name,
                "sort_order": sort_order,
            },
        )

    # Written out rather than interpolated. There are exactly two referencing
    # columns, and a loop over table names means building SQL by f-string —
    # which bandit flags and which cannot be fixed with a bound parameter,
    # because an identifier is not a value. Two literal statements need no
    # suppression and read no worse.
    repoint_education = sa.text(
        "UPDATE education_entries SET degree_level_id = "
        "(SELECT id FROM degree_levels WHERE slug = :into_slug) "
        "WHERE degree_level_id = (SELECT id FROM degree_levels WHERE slug = :from_slug)"
    )
    repoint_goals = sa.text(
        "UPDATE mentee_goals SET degree_goal_id = "
        "(SELECT id FROM degree_levels WHERE slug = :into_slug) "
        "WHERE degree_goal_id = (SELECT id FROM degree_levels WHERE slug = :from_slug)"
    )
    for from_slug, into_slug in MERGES:
        slugs = {"into_slug": into_slug, "from_slug": from_slug}
        op.get_bind().execute(repoint_education, slugs)
        op.get_bind().execute(repoint_goals, slugs)
        op.get_bind().execute(
            sa.text("DELETE FROM degree_levels WHERE slug = :slug"), {"slug": from_slug}
        )


def downgrade() -> None:
    """Downgrade schema.

    Restores the six rows and their names. It does **not** put the merged rows
    back: once an MBA is recorded as a master's degree, nothing here knows which
    master's degrees were MBAs. That is what merging means, and
    `education_entries.degree_category` is where the original string survives.
    """
    rename = sa.text(
        "UPDATE degree_levels SET slug = :new_slug, display_name = :display_name, "
        "sort_order = :sort_order WHERE slug = :old_slug"
    )
    for old_slug, new_slug, display_name, sort_order in (
        ("diploma", "diploma", "Diploma", 20),
        ("bachelors", "undergraduate", "Undergraduate", 10),
        ("masters", "masters", "Masters", 30),
        ("doctorate", "phd", "PhD", 50),
    ):
        op.get_bind().execute(
            rename,
            {
                "old_slug": old_slug,
                "new_slug": new_slug,
                "display_name": display_name,
                "sort_order": sort_order,
            },
        )
    op.execute(
        "INSERT INTO degree_levels (slug, display_name, sort_order) "
        "VALUES ('mba', 'MBA', 40), ('postdoc', 'Postdoctoral', 60)"
    )
