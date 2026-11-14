"""Record how an analytics-consent answer was given (decision 345).

``analytics_consents.method`` (nullable, no default, a ``CHECK`` of the four
``ConsentMethod`` values): ``accept``, ``decline``, ``close`` (the banner's X,
essential only) or ``settings``. Every existing row stays null, since nothing
says how those answers were given.

Expand only: adding a nullable column without a default is a catalogue change,
and the ``CHECK`` passes every existing row because they are all null. Running
code writes and reads it from the release that ships with it.

Downgrade drops the column and its constraint; the methods recorded since are
lost, and the answers themselves are kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c2e4a91f30"
down_revision: str | Sequence[str] | None = "e8b2c5f1a7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column("analytics_consents", sa.Column("method", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_analytics_consents_method_is_known"),
        "analytics_consents",
        "method IS NULL OR method IN ('accept', 'decline', 'close', 'settings')",
    )
    # The fit rule too, for writers that bypass the API (decision 345).
    op.create_check_constraint(
        op.f("ck_analytics_consents_method_fits_answer"),
        "analytics_consents",
        "method IS NULL OR method = 'settings' OR (method = 'accept') = granted",
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(
        op.f("ck_analytics_consents_method_fits_answer"), "analytics_consents", type_="check"
    )
    op.drop_constraint(
        op.f("ck_analytics_consents_method_is_known"), "analytics_consents", type_="check"
    )
    op.drop_column("analytics_consents", "method")
