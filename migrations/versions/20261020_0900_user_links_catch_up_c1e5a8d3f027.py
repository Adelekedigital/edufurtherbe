"""Copy what the old link columns gained since ``d6f2a9c4e185`` (decision 275).

That migration copied ``social_twitter`` and ``social_youtube`` into
``user_links`` while both columns still had writers: the profile PATCH and the
Bubble loader. The release that ships with this migration moves both writers to
the table, so a value written to a column in between is the only thing not yet
there. This copies it.

**Run after the code deploys**: from then on nothing writes the columns, so the
copy is complete. **A column value wins over the first copy**: the new writer
clears a platform's column on every write and delete, so a column still holding
a value is the old writer's, and either equals what was first copied or is a
later edit. **Idempotent**: a second run writes the same urls again.

**One case it cannot see, and why it does not arise**: a link cleared through the
old PATCH in the window leaves its first copy behind. Only a database that ran
``d6f2a9c4e185`` before this release has a window, and staging's copied no links
(it holds no X or YouTube values); a new database runs the chain before any
code, so it has no window at all.

The rule is the one ``d6f2a9c4e185`` froze, loaded from that file rather than
copied a second time: a migration is fixed history, so it cannot drift.

Data only, no schema change; the downgrade does nothing, because the rows it
copied are indistinguishable from links written through the API.
"""

import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e5a8d3f027"
down_revision: str | Sequence[str] | None = "e8b3c5f1a726"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = Path(__file__).with_name("20261019_1200_user_links_d6f2a9c4e185.py")

#: Each old column, the platform it becomes, and its place in the order.
COLUMNS = (("social_twitter", "x", 0), ("social_youtube", "youtube", 1))


def _frozen_rule() -> Callable[[str, str | None], str | None]:
    spec = importlib.util.spec_from_file_location("user_links_backfill", _BACKFILL)
    assert spec is not None and spec.loader is not None  # noqa: S101 — a fixed sibling file
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.canonical  # type: ignore[no-any-return]


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    canonical = _frozen_rule()
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT p.user_id, p.social_twitter, p.social_youtube FROM user_profiles p "
            "WHERE p.social_twitter IS NOT NULL OR p.social_youtube IS NOT NULL"
        )
    ).mappings()
    links: list[dict[str, object]] = []
    for row in rows:
        for column, platform, position in COLUMNS:
            url = canonical(platform, row[column])
            if url is not None:
                links.append(
                    {"user_id": row["user_id"], "platform": platform, "url": url, "pos": position}
                )
    written = 0
    for link in links:
        written += (
            bind.execute(
                sa.text(
                    "INSERT INTO user_links (user_id, platform, url, position) "
                    "VALUES (:user_id, :platform, :url, :pos) "
                    "ON CONFLICT (user_id, platform) WHERE platform <> 'website' "
                    "DO UPDATE SET url = EXCLUDED.url"
                ),
                link,
            ).rowcount
            or 0
        )
    print(f"user_links catch-up: wrote {written} link(s) from {len(links)} readable value(s)")


def downgrade() -> None:
    """Nothing: the copied rows cannot be told apart from links written since."""
