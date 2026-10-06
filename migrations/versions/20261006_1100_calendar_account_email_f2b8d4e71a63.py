"""Name the Google account a mentor connected: ``calendar_connections.external_account_email``.

ADR 0012 as amended on 2026-10-06, while it was still ``Proposed``. The mentor
consent now asks for ``openid email`` beside ``calendar.freebusy``, so the grant
can say *which* account it is — which `calendar.freebusy` cannot answer on its
own, `calendarList.list` being outside it.

**Why not ``external_account_id``, which already exists.** That column is for the
account's stable identifier, the ``sub`` claim, and its comment says so. An email
is not an id — it can change while ``sub`` cannot — and putting one in a column
named for the other is the kind of thing that reads correctly for a year and then
misleads somebody at the worst moment. ``external_account_id`` stays null,
because nothing reads an id yet and a column filled for no reader is how the
*next* person concludes a field is load-bearing.

**Nullable with no backfill, and null is permanent for older rows.** A scope
change does not retro-fit a grant: a mentor who consented before this has no
email in their token and gets one only by consenting again. There is nothing to
backfill from — no source, nowhere to read it — so null means *not known* rather
than *none*, and a client degrades to "Connected".

At the time of writing no environment had a calendar client configured and no
mentor had connected anything, so in practice there are no older rows anywhere.
That is also why the scope widened now rather than later: afterwards the same
change costs every connected mentor a re-consent.

Expand only. Adding a nullable column takes a brief ``ACCESS EXCLUSIVE`` lock and
rewrites nothing, so both code versions serve during the deploy — the old one
never selects it, the new one reads null until a mentor reconnects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b8d4e71a63"
down_revision: str | Sequence[str] | None = "d4b7e2a91c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "calendar_connections"
COLUMN = "external_account_email"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column.

    Nothing is lost that cannot be read again: the value came from Google and
    comes back on the next consent. The connection itself — the refresh token,
    which is the thing a mentor cannot simply re-derive — is untouched.
    """
    op.drop_column(TABLE, COLUMN)
