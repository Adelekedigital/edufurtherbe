"""The provisioning store's statements, checked without a database.

The live-user predicate is **not** asserted here any more — it moved to
``test_predicates.py`` when a second store started using it, so one rule has one
check as well as one representation. What is left is specific to these
statements: the conflict target, and the guard that stops a concurrent run
overwriting an identifier.
"""

from sqlalchemy.dialects import postgresql

from app.infra.db import provisioning_store

PREDICATE = "deleted_at IS NULL"


def sql(statement: object) -> str:
    """The statement as PostgreSQL will see it."""
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_the_grant_conflict_target_matches_the_partial_index() -> None:
    """``ix_admin_users_active_grant`` is ``(user_id, admin_role) WHERE revoked_at
    IS NULL``. Inference resolves only if the target matches on both, and a
    mismatch is a runtime error during a grant rather than anything visible here
    — so the compiled clause is asserted."""
    compiled = sql(provisioning_store.GRANT)

    assert "ON CONFLICT (user_id, admin_role) WHERE revoked_at IS NULL DO NOTHING" in compiled


def test_the_link_statement_cannot_overwrite_an_existing_identifier() -> None:
    """``auth_id IS NULL`` is what makes a second concurrent run a no-op rather
    than a clobber that orphans a working account."""
    assert "auth_id IS NULL" in sql(provisioning_store.LINK)
