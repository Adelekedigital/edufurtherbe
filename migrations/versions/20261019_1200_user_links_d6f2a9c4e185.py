"""A user's social links, one row each: ``user_links`` (decision 275).

Mentor Settings PRD, owner 2026-10-19. A profile carried two link columns,
``social_twitter`` and ``social_youtube``; the settings page needs six platforms
(X, YouTube, Instagram, Facebook, TikTok, and up to three websites). A column
per platform does not hold three websites, so links become rows.

**LinkedIn is not moved.** ``social_linkedin`` stays on ``user_profiles``,
because the mentor application, its review freeze and onboarding read it.

``url`` is the canonical ``https://`` link (#182), never what was typed.
``ix_user_links_one_per_platform`` is partial, so a second X link is refused
while websites are left to the writer's count of three. ``kind`` admits only
``social``; a custom link joins when something writes one (#21). Cascades with
the user: a link is evidence of nothing.

**The backfill copies what reads publish today.** Every ``social_twitter`` and
``social_youtube`` value goes through the rule ``domain.social_links`` applies
on read, frozen below because a migration must not import a live symbol (#105)
and pinned to the live rule by a test. X lands at position 0 and YouTube at 1.
A value the rule cannot read already publishes as ``null`` (#182), so skipping
it hides nothing; it stays in its column, and the count is printed. The old
columns are kept, unwritten, for one release and then dropped.

Expand only, and merged alone (project-conventions, *Migration ordering*):
nothing reads or writes the table until the next PR, so this lands inert.
"""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f2a9c4e185"
down_revision: str | Sequence[str] | None = "d2f8b6a4c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_links"

#: The longest input the rule reads (``social_links.MAX_LENGTH`` when frozen).
MAX_LENGTH = 2048

# --- The X and YouTube halves of `domain.social_links`, frozen on 2026-10-19 ---
_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}", re.ASCII)
_YOUTUBE_HANDLE = re.compile(r"[A-Za-z0-9_.-]{3,30}", re.ASCII)
_YOUTUBE_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}", re.ASCII)
_SCHEME = re.compile(r"https?://", re.IGNORECASE | re.ASCII)
_HOSTS = {
    "x": frozenset(
        {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com",
         "mobile.twitter.com"}
    ),
    "youtube": frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"}),
}  # fmt: skip
_X_RESERVED = frozenset(
    {
        "home", "i", "intent", "share", "search", "explore", "settings", "messages",
        "notifications", "hashtag", "login", "signup", "tos", "privacy", "compose",
    }
)  # fmt: skip


def _from_handle(network: str, handle: str) -> str | None:
    if network == "x":
        if _X_HANDLE.fullmatch(handle) and handle.lower() not in _X_RESERVED:
            return f"https://x.com/{handle}"
        return None
    if _YOUTUBE_HANDLE.fullmatch(handle):
        return f"https://www.youtube.com/@{handle}"
    return None


def canonical(network: str, value: str | None) -> str | None:
    """``canonical_social`` for ``x`` and ``youtube``, as it read on 2026-10-19."""
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > MAX_LENGTH or not text.isprintable() or " " in text:
        return None
    if not (_SCHEME.match(text) or "/" in text):
        return _from_handle(network, text.removeprefix("@"))
    if not _SCHEME.match(text):
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if "@" in parts.netloc or port is not None or parts.hostname is None:
        return None
    host = parts.hostname.rstrip(".")
    segments = [s for s in parts.path.split("/") if s]
    if host not in _HOSTS[network] or not segments:
        return None
    if network == "x":
        return _from_handle(network, segments[0])
    first = segments[0]
    if first.startswith("@"):
        return _from_handle(network, first[1:])
    if first == "channel" and len(segments) > 1 and _YOUTUBE_CHANNEL.fullmatch(segments[1]):
        return f"https://www.youtube.com/channel/{segments[1]}"
    return None


# --- end of the frozen rule ---

#: Each old column, the platform it becomes, and the position it lands at.
COLUMNS = (("social_twitter", "x", 0), ("social_youtube", "youtube", 1))


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'social'"), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint(
            "platform IN ('x', 'youtube', 'instagram', 'facebook', 'tiktok', 'website')",
            name=op.f("ck_user_links_platform_is_known"),
        ),
        sa.CheckConstraint("kind IN ('social')", name=op.f("ck_user_links_kind_is_known")),
        sa.CheckConstraint(
            f"url LIKE 'https://%' AND char_length(url) <= {MAX_LENGTH}",
            name=op.f("ck_user_links_url_shape"),
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_user_links_position_not_negative")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_links")),
    )
    op.create_index(
        "ix_user_links_one_per_platform",
        TABLE,
        ["user_id", "platform"],
        unique=True,
        postgresql_where=sa.text("platform <> 'website'"),
    )
    op.create_index("ix_user_links_user_order", TABLE, ["user_id", "position", "created_at"])
    op.execute(
        f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    _backfill()


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, social_twitter, social_youtube FROM user_profiles "
            "WHERE social_twitter IS NOT NULL OR social_youtube IS NOT NULL"
        )
    ).mappings()
    links: list[dict[str, object]] = []
    skipped = 0
    for row in rows:
        for column, platform, position in COLUMNS:
            if row[column] is None:
                continue
            url = canonical(platform, row[column])
            if url is None:
                skipped += 1
                continue
            links.append(
                {"user_id": row["user_id"], "platform": platform, "url": url, "pos": position}
            )
    if links:
        bind.execute(
            sa.text(
                "INSERT INTO user_links (user_id, platform, url, position) "
                "VALUES (:user_id, :platform, :url, :pos)"
            ),
            links,
        )
    print(f"user_links: copied {len(links)} link(s); {skipped} unreadable value(s) left in place")


def downgrade() -> None:
    """Put X and YouTube back into their columns, then drop the table.

    **Refuses while a user holds a link the columns cannot carry**: Instagram,
    Facebook, TikTok or a website would be lost, and that is an operator's call
    made before downgrading. X and YouTube go back in their canonical form,
    which is what a read published; a column whose value the backfill could not
    read was never copied, and keeps it.
    """
    op.execute("SET lock_timeout = '3s'")
    bind = op.get_bind()
    others = bind.execute(
        sa.text("SELECT count(*) FROM user_links WHERE platform NOT IN ('x', 'youtube')")
    ).scalar_one()
    if others:
        raise RuntimeError(
            f"{others} link(s) on Instagram, Facebook, TikTok or a website have no "
            "column to return to; downgrading would delete them"
        )
    bind.execute(
        sa.text(
            "UPDATE user_profiles p SET social_twitter = l.url FROM user_links l "
            "WHERE l.user_id = p.user_id AND l.platform = 'x'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE user_profiles p SET social_youtube = l.url FROM user_links l "
            "WHERE l.user_id = p.user_id AND l.platform = 'youtube'"
        )
    )
    op.drop_table(TABLE)
