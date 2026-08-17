"""The 24-hour booking notice the migration was not carrying.

Legacy enforced a platform rule: **no same-day booking, 24 hours' notice.** The
column that expresses it, ``session_type_booking_configs.min_notice_minutes``,
defaults to **120** — two hours — and nothing has ever written it. The ETL does
not set it and no endpoint can, so every migrated offering sits on that default
and would permit booking two hours out against a rule of twenty-four.

Nobody chose that. It is a column default nobody revisited, and it is invisible
because 120 is a perfectly valid value: every count reconciles, every test
passes, and ``/slots`` simply starts offering times legacy would never have
shown.

**This is the first change that makes the product less permissive for existing
users.** Migrated mentors become unbookable inside 24 hours the moment it
deploys, and none of them will be told. It restores a rule they were already
operating under rather than imposing a new one, which is the argument for doing
it before booking ships rather than after.

WHERE THE RULE LIVES, AND WHY NOT HERE
======================================
The ``CHECK`` below is **sanity, not policy**. It refuses a negative and refuses
beyond thirty days; it does **not** encode the 24-hour floor.

The product rule — 24 hours minimum, 72 hours maximum, the mentor's choice per
session type — belongs at the Pydantic boundary, and lands with the write schema
in the ``POST``/``PATCH`` PR. Three reasons:

* **The API is the only writer this column will ever have.** The boundary is the
  enforcement point in practice.
* ``booking_policies`` is already in the canonical package. When the 24—72 range
  moves there it is a config change; a ``CHECK`` carrying the product rule would
  need a migration every time the product changed its mind.
* A database refuses what is *impossible*; an application refuses what is
  *disallowed*. Settled decision #100 already puts closed-set enforcement at the
  Pydantic edge for the same reason.

Thirty days rather than the seven first proposed: a sanity bound must never be
the thing blocking a product decision, and the 72-hour ceiling is already
expected to rise.

**This diverges from canonical**, which specifies ``DEFAULT 120``
(``04_sessions.sql``). Recorded as a settled decision rather than an ADR,
following the precedent set for ``meeting_venue`` — canonical says
``NULL = inherit from mentor`` and #102 made it ``NOT NULL``.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "f1b6a92c7d4e"
down_revision = "d1f8a3c62b47"
branch_labels = None
depends_on = None

PLATFORM_FLOOR = 1440  # 24 hours
SANITY_CEILING = 43200  # 30 days
CHECK_NAME = "ck_session_type_booking_configs_min_notice_minutes_sane"


def upgrade() -> None:
    # **Before the default changes, and it must be unconditional.** Altering a
    # column default does not touch existing rows, so without this every
    # migrated offering keeps 120. Every row is rewritten rather than only those
    # matching the old default, because nothing has ever written this column —
    # so a 120 cannot be somebody's deliberate choice, and matching on the value
    # would be asserting a distinction that does not exist.
    op.execute(
        text("UPDATE session_type_booking_configs SET min_notice_minutes = :floor").bindparams(
            floor=PLATFORM_FLOOR
        )
    )
    op.execute(
        f"ALTER TABLE session_type_booking_configs "
        f"ALTER COLUMN min_notice_minutes SET DEFAULT {PLATFORM_FLOOR}"
    )
    # `op.f()` with the **fully rendered** name, matching every other constraint
    # in this schema. Two reasons, and the second is not obvious:
    #
    # `op.f()` marks a name as final so the naming convention is not applied to
    # it again — a bare name passed to `drop_constraint` gets the convention
    # applied, which is how the contract step double-prefixed one and truncated
    # it at 63 characters.
    #
    # And `test_every_schema_identifier_appears_verbatim_in_source` requires the
    # rendered name to appear **literally** somewhere in `src/` or `migrations/`.
    # A bare name leaves the database holding an identifier nobody can grep for,
    # which is exactly the state that test exists to prevent.
    op.create_check_constraint(
        op.f(CHECK_NAME),
        "session_type_booking_configs",
        f"min_notice_minutes BETWEEN 0 AND {SANITY_CEILING}",
    )


def downgrade() -> None:
    """Restores the shape and **cannot restore the values.**

    Which offerings were on 120 because nobody set it — all of them, today — is
    not recorded anywhere afterwards, and by the time this runs a mentor may have
    chosen 48 hours deliberately. Re-defaulting to 120 would be inventing data,
    so the values are left where they are and only the default and the constraint
    move back.
    """
    op.drop_constraint(op.f(CHECK_NAME), "session_type_booking_configs", type_="check")
    op.execute(
        "ALTER TABLE session_type_booking_configs ALTER COLUMN min_notice_minutes SET DEFAULT 120"
    )
