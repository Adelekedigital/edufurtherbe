"""email normalised at the boundary — citext out, CHECK in

``users.email`` becomes ``text`` with ``CHECK (email = lower(email))``, and the
``citext`` extension is dropped.

WHY THE REVERSAL
================
M1 adopted ``citext`` from the package so that ``WHERE email = :x`` would be
case-insensitive at every call site rather than at the ones somebody remembered
to ``lower()``. That argument is real, but it solves the problem twice: **every
writer already normalises.** The ETL transform lowercases and strips before a row
is built, and the API schema does the same before a request reaches a handler.
Normalisation belongs at the boundary, and once it is there the stored value is
canonical by construction — a case-insensitive *type* is a second mechanism for
an invariant already held.

The ``CHECK`` is what makes that explicit rather than assumed. It is not
redundant with the boundary: it is what fails loudly when a future writer
forgets — precisely the case ``citext`` was insuring against, except it now
fails instead of silently storing a value nobody can find again.

WHAT THIS ALSO REMOVES
======================
``citext`` was the only object in the chain whose behaviour on the target
platform was unverified. Supabase installs extensions into an ``extensions``
schema and lints those created in ``public`` (advisor
``0014_extension_in_public``: "the extension's internal functions and tables are
visible in your API"). Our migration created it unqualified, so it would have
landed in ``public`` and tripped that lint. The lint's stated risk is PostgREST
exposure, which ADR 0005 already removed by not using PostgREST — so it was a
warning we could have accepted, but not generating one is better than accepting
one.

It also leaves one fewer non-standard type for ``compare_metadata`` to reflect,
which was the remaining question ``alembic check`` could not answer without a
real project.

A FORWARD MIGRATION, NOT AN AMENDMENT
=====================================
M1 could have been edited instead — ADR 0015 used that exception for M0 when
nothing was deployed, and nothing is deployed now either. It is deliberately not
used again. That exception was written as one-time, and a chain whose merged
revisions keep changing stops being a record of anything. Two statements forward
are honest: the history shows citext was adopted and then reconsidered, which is
what happened.

Revision ID: e25541374c03
Revises: 1aa10cb07322
Create Date: 2026-08-06 01:14:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e25541374c03"
down_revision: str | Sequence[str] | None = "1aa10cb07322"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare name. The `ck` convention on Base.metadata renders the `ck_users_` prefix,
# and passing the rendered form produces `ck_users_ck_users_...` — a defect that
# shipped once in M1 and is now caught by a parity test.
CONSTRAINT = "email_is_lowercase"


def upgrade() -> None:
    """Upgrade schema.

    ``USING email::text`` is required: PostgreSQL will not implicitly cast citext
    to text in an ``ALTER COLUMN TYPE``. Without it this fails with "column
    cannot be cast automatically", which reads like a type-system complaint and
    is really three missing words.

    The table rewrite this causes is irrelevant at 1,200 rows and would matter at
    a million. Called out because the same statement is not free later.
    """
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    op.execute("ALTER TABLE users ALTER COLUMN email TYPE text USING email::text")
    op.create_check_constraint(CONSTRAINT, "users", "email = lower(email)")

    # Safe only because nothing uses the type any more — that column was its
    # single consumer. `IF EXISTS` because a database provisioned after this
    # revision never had it.
    op.execute("DROP EXTENSION IF EXISTS citext")


def downgrade() -> None:
    """Downgrade schema.

    Recreates the extension before restoring the type, because the type cannot
    exist without it. The reverse order fails with "type citext does not exist" —
    obvious in hindsight, and the sort of thing an untested downgrade ships.

    **Lossy in one direction that does not matter and one that does.** Reverting
    the column type loses nothing: every stored value is already lowercase, which
    citext accepts. Dropping the CHECK loses the guarantee that it stays that
    way, so a database downgraded and left there will accept mixed case again —
    and re-upgrading will then fail on the CHECK rather than silently correcting
    the data. That is the honest behaviour: it refuses rather than rewriting rows
    nobody asked it to touch.
    """
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '60s'")

    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    # Bare name here too. `drop_constraint` applies the naming convention just
    # as `create_check_constraint` does, so passing the rendered
    # `ck_users_email_is_lowercase` asks it to drop
    # `ck_users_ck_users_email_is_lowercase`. That is the same double-render
    # that shipped in M1 — written again here, in the migration whose comment
    # warns about it, because the warning was attached to the *create* call
    # and this is the drop. Caught by the up/down/up test, which is the only
    # thing that exercises a downgrade at all.
    op.drop_constraint(CONSTRAINT, "users", type_="check")
    op.execute("ALTER TABLE users ALTER COLUMN email TYPE citext USING email::citext")
