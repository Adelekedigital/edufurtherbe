"""M4 degree short forms, and the abbreviation a user actually holds

THE PROBLEM
===========
The mentor card renders "Ph.D, Mathematics, Washington University". Nothing in
this schema could produce the first element. `degree_levels` held `slug`
(`doctorate`) and `display_name` (`Doctorate (PhD)`), and neither is what a card
shows.

The obvious fix — one short form per level — is the mistake this table was
reshaped to remove. The ISCED migration (b83f0e51d7a2) deleted the `mba` row for
being *"not a level — a specific master's degree"*, and recorded why the level
must stay generic: **"a Nigerian BSc, a UK BA and a US Bachelor's are the same
level and three different words."** Putting `B.Sc` on the bachelors row
re-introduces exactly that, and renders `B.Sc, Law` for every law graduate.

THREE COLUMNS, THREE JOBS
=========================
    degree_levels.short_name    the generic fallback, shown when nothing was
                                chosen: Ph.D / Master's / Bachelor's / Diploma
    degree_levels.short_forms   the menu a client offers: {B.Sc, B.A, LL.B, ...}
    education_entries
      .degree_abbreviation      what this user actually holds; NULL inherits

**The fallback is deliberately not a member of the menu.** "Nothing chosen"
renders the level generically, which is always true, rather than guessing a
specific abbreviation that is wrong for every user outside one field. Advisory
rather than a foreign key, because a user may type `M.Litt` and the alternative
is carrying both an id and a text column for one fact.

`short_forms` follows `institutions.alt_names` — a `text[]` of alternative
strings hanging off a catalogue row. Same problem, same shape.

WHY THE ABBREVIATION COLUMN LANDS NOW
=====================================
Legacy `Education.shortForm` is populated on **21 of 21** rows of the dev export
and the M2 transform reads none of it. After cutover the Bubble data is gone and
the value cannot be re-fetched — the same reasoning `education_entries` already
records for keeping `degree_category` beside `degree_level_id`. Deferring this
column does not postpone it; it destroys the data.

THE SEEDS ARE A SUPERSET OF THE EXPORT, MEASURED NOT GUESSED
============================================================
Every abbreviation the migration will write must appear in its level's menu, or
a client's dropdown cannot show a user the value they already have. Counted
across the dev export:

    Bachelors    BSc x11, B.sc x2, B.Eng x1, LL.B x1
    Masters      M.Sc x2, MSc x1, M.Eng x1
    Doctorates   Ph.D x1
    Diploma      HND x1

Note `BSc`/`B.sc` and `M.Sc`/`MSc` — the same degree spelled two ways, differing
by **dots as well as case**. The transform folds them on a
strip-punctuation-and-casefold key; the canonical spelling is dotted, matching
the product design and the legacy card.

NO `NOT NULL` ON `degree_abbreviation`
======================================
NULL means *inherit*, which is a state the column must be able to hold. It is
the same null-means-inherit rule as `session_type_booking_configs.meeting_venue`
(D21).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4e91a7d3f52"
down_revision: str | Sequence[str] | None = "a1c7e46b02d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (slug, short_name, short_forms). Ascending by ISCED level, as the table is
#: ordered. Each menu contains every value the export holds for that level.
LEVELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("diploma", "Diploma", ("Diploma", "Certificate", "HND", "Advanced Diploma")),
    ("bachelors", "Bachelor's", ("B.Sc", "B.A", "B.Eng", "LL.B", "B.Com", "B.Ed", "MBBS")),
    ("masters", "Master's", ("M.Sc", "M.A", "M.Eng", "M.Ed", "M.Phil", "MBA", "LL.M")),
    ("doctorate", "Ph.D", ("Ph.D", "M.D", "J.D", "Ed.D", "D.Phil")),
)


def upgrade() -> None:
    """Upgrade schema.

    **Bound parameters, never interpolation.** `Bachelor's` and `Master's` carry
    apostrophes, and the ISCED migration records that its first version built the
    same kind of statement with `%`-formatting, suppressed ruff's `S608` to do
    it, and broke on `Bachelor's Degree`. That warning was right about the line
    it was suppressed on.

    **Added nullable, backfilled, then constrained** — three steps for four rows,
    where one `ALTER` would have worked. The size of the table is not the reason
    the pattern exists: a column added `NOT NULL` without a default fails against
    any table that already has rows, and the next person copies the shape they
    find, not the row count.
    """
    op.add_column("degree_levels", sa.Column("short_name", sa.Text(), nullable=True))
    op.add_column(
        "degree_levels",
        sa.Column(
            "short_forms",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )

    seed = sa.text(
        "UPDATE degree_levels SET short_name = :short_name, short_forms = :short_forms "
        "WHERE slug = :slug"
    ).bindparams(sa.bindparam("short_forms", type_=postgresql.ARRAY(sa.Text())))
    for slug, short_name, short_forms in LEVELS:
        op.get_bind().execute(
            seed, {"slug": slug, "short_name": short_name, "short_forms": list(short_forms)}
        )

    op.alter_column("degree_levels", "short_name", nullable=False)

    op.add_column("education_entries", sa.Column("degree_abbreviation", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Reversible without loss *of schema*. The abbreviations themselves are not
    recoverable by re-running the transform after cutover, which is the whole
    argument for adding the column while the export still exists.
    """
    op.drop_column("education_entries", "degree_abbreviation")
    op.drop_column("degree_levels", "short_forms")
    op.drop_column("degree_levels", "short_name")
