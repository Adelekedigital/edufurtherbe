"""Drop ``user_profiles.social_twitter`` and ``social_youtube`` (decision 275, #505).

The contract step. Nothing reads or writes the columns since the release that
removed their API fields, so they go, but **only after one more copy**: the
catch-up ``c1e5a8d3f027``'s own, run again here rather than written a second
time. A column still holding a value is one no link write has touched (every
add, edit and delete clears its platform's column), so the copy can never bring
back a link somebody deleted, and it is idempotent where the catch-up already
ran. A value the frozen rule cannot read already published as ``null`` and goes
with its column.

**Run after the code deploys**, as the catch-up was: the code that ships with
this migration no longer names the columns.

The downgrade re-adds both columns, nullable as they were, and fills them from
each user's X and YouTube links where a profile row exists. A link on a user
with no profile row stays only in ``user_links``, which every reader of that
release used anyway.
"""

import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a7c2d9b318"
down_revision: str | Sequence[str] | None = "c1e5a8d3f027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATCH_UP = Path(__file__).with_name("20261020_0900_user_links_catch_up_c1e5a8d3f027.py")

#: Each old column and the platform it became, as the catch-up names them.
COLUMNS = (("social_twitter", "x"), ("social_youtube", "youtube"))


def _catch_up() -> Callable[[], None]:
    spec = importlib.util.spec_from_file_location("user_links_catch_up", _CATCH_UP)
    assert spec is not None and spec.loader is not None  # noqa: S101 — a fixed sibling file
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.upgrade  # type: ignore[no-any-return]


def upgrade() -> None:
    _catch_up()()
    for column, _ in COLUMNS:
        op.drop_column("user_profiles", column)


#: Written out rather than built from `COLUMNS`: no SQL is assembled from strings.
_RESTORE = (
    "UPDATE user_profiles p SET social_twitter = l.url FROM user_links l "
    "WHERE l.user_id = p.user_id AND l.platform = 'x'",
    "UPDATE user_profiles p SET social_youtube = l.url FROM user_links l "
    "WHERE l.user_id = p.user_id AND l.platform = 'youtube'",
)


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for column, _ in COLUMNS:
        op.add_column("user_profiles", sa.Column(column, sa.Text(), nullable=True))
    for statement in _RESTORE:
        op.execute(statement)
