"""Drop ``user_profiles.social_twitter`` and ``social_youtube`` (decision 275, #505).

The contract step. Nothing reads or writes the columns since the release that
removed their API fields, and **nothing is copied first, deliberately**. Every
readable value was copied into ``user_links`` by ``d6f2a9c4e185``, and what the
old writers added afterwards by the catch-up ``c1e5a8d3f027``, which ran after
the code that moved every writer to the table. Since then no writer has touched
the columns, so a value still in one is either a stale original (its link has
been edited since, through the API or by an import re-run that writes the
table only) or one the rule cannot read (it already published as ``null``).
Copying again could only roll a link back to the stale value, or bring back a
link its owner deleted after this release stopped clearing the columns.

**Deploy the code first, then run Migrate**, as #442 did: the release before
this one still maps the columns and clears them on every link write, and would
fail once they are gone. A rollback below this release downgrades this
migration first.

The downgrade re-adds both columns, nullable as they were, and fills them from
each user's X and YouTube links where a profile row exists. A link on a user
with no profile row stays only in ``user_links``, which every reader of that
release used anyway.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a7c2d9b318"
down_revision: str | Sequence[str] | None = "c1e5a8d3f027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each column restored on the way down, from its platform's link. The one place
#: the pairing appears in this file.
_RESTORE = (
    (
        "social_twitter",
        "UPDATE user_profiles p SET social_twitter = l.url FROM user_links l "
        "WHERE l.user_id = p.user_id AND l.platform = 'x'",
    ),
    (
        "social_youtube",
        "UPDATE user_profiles p SET social_youtube = l.url FROM user_links l "
        "WHERE l.user_id = p.user_id AND l.platform = 'youtube'",
    ),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for column, _ in _RESTORE:
        op.drop_column("user_profiles", column)


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for column, restore in _RESTORE:
        op.add_column("user_profiles", sa.Column(column, sa.Text(), nullable=True))
        op.execute(restore)
