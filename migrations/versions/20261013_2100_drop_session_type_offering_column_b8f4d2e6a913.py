"""Drop ``session_types.service_offering_id``: the contract step of #266 (#274).

A type's offerings live in ``session_type_offerings`` (#266). This column was
the single offering from before the set; it has been dual-written, then read
by nothing (#441), and every column-only type was given its set first
(``a7e3c1d9b452``). Nothing reads or writes it now, so it and its foreign key
go.

**Run after the code that stops writing it is deployed** (the PR this lands in
removes the dual-write and the model attribute): the app never touches the
column, so the order between the deploy and this migration is safe either way
round, but code from before #441 would fail once it is gone, which is why the
expand steps came first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8f4d2e6a913"
down_revision: str | Sequence[str] | None = "a7e3c1d9b452"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_types"
COLUMN = "service_offering_id"
#: The name `e5a71c9b382d` gave it; restated, because a migration must not
#: import another's module.
FK_NAME = "fk_session_types_service_offering_id_service_offerings"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
    op.drop_column(TABLE, COLUMN)


def downgrade() -> None:
    """Bring the column back, refilled from each type's set.

    Faithful: the column only ever held the set's first item (#266), so
    position 0 is exactly what it said. A type with no set gets `NULL`, as an
    unclassified type had.
    """
    op.execute("SET lock_timeout = '3s'")
    op.add_column(TABLE, sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FK_NAME, TABLE, "service_offerings", [COLUMN], ["id"], ondelete="RESTRICT"
    )
    op.execute(
        "UPDATE session_types t SET service_offering_id = o.service_offering_id "
        "FROM session_type_offerings o "
        "WHERE o.session_type_id = t.id AND o.position = 0"
    )
