"""Drop ``session_types.service_offering_id``: the contract step of #266 (#274).

A type's offerings live in ``session_type_offerings`` (#266). This column was
the single offering from before the set; it has been dual-written, then read
by nothing (#441), and every column-only type was given its set first
(``a7e3c1d9b452``). Nothing reads or writes it now, so it and its foreign key
go.

**Order matters: deploy the code first, then run this.** The PR this lands in
removes the model attribute and the dual-write; code from before it (#441 and
earlier) still maps and writes the column and would fail on every session-type
read once it is gone. So `Migrate` runs only after this release is live, and a
rollback below it means downgrading this migration first.

**The backfill runs once more before the drop**, under the same lock as
``a7e3c1d9b452``: anything that gave a type its offering only in the column since
then (a pre-#439 seed still deployed, a hand fix) is carried into the set
instead of being lost with the column.
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
    op.execute("LOCK TABLE session_type_offerings IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        "INSERT INTO session_type_offerings (session_type_id, service_offering_id, position) "
        "SELECT t.id, t.service_offering_id, 0 FROM session_types t "
        "WHERE t.service_offering_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM session_type_offerings o WHERE o.session_type_id = t.id) "
        "ON CONFLICT (session_type_id, service_offering_id) DO NOTHING"
    )
    op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
    op.drop_column(TABLE, COLUMN)


def downgrade() -> None:
    """Bring the column back, refilled from each type's set.

    Faithful because the upgrade backfilled first: every type whose column held
    a value has a set, and the column only ever held the set's first item
    (#266), so position 0 is exactly what it said. A type with no set gets
    `NULL`, as an unclassified type had. (A set a mentor edits after the drop is
    restored as edited, which is the truth at that point.)
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
