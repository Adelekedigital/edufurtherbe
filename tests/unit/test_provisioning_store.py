"""The store's statements, checked without a database.

**This file exists because prose did not work.** "A soft-deleted user is
invisible" was written down, understood, and applied to four of the five
statements that needed it. The one that got missed was the ``UPDATE``, so a user
soft-deleted mid-run would have been handed a live Supabase account — found by a
reviewer, not by any gate, because a predicate inside SQL is invisible to mypy,
to ruff and to the layer check alike.

A rule with no check is a rule with a countdown on it. This is the check.
"""

import inspect

from sqlalchemy import Delete, Select, Update
from sqlalchemy.dialects import postgresql

from app.infra.db import provisioning_store
from app.infra.db.provisioning_store import USER_STATEMENTS

PREDICATE = "deleted_at IS NULL"


def sql(statement: object) -> str:
    """The statement as PostgreSQL will see it."""
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_no_statement_touching_an_existing_user_omits_the_live_predicate() -> None:
    """The rule, asserted rather than remembered."""
    for statement in USER_STATEMENTS:
        assert PREDICATE in sql(statement), sql(statement)


def test_every_statement_that_filters_users_is_declared() -> None:
    """The completeness half, and the one that keeps this honest.

    Checking only the declared tuple would pass forever against a new statement
    that nobody added to it — the list would guard itself. This walks the module
    instead: anything that reads or updates existing ``users`` rows must appear in
    ``USER_STATEMENTS``, so adding a statement and forgetting to declare it fails
    here rather than in a cutover.

    ``INSERT`` is exempt on purpose. It creates the row, so there is no existing
    row for a soft-delete predicate to be about.
    """
    filtering = {
        name: value
        for name, value in inspect.getmembers(provisioning_store)
        if isinstance(value, Select | Update | Delete)
    }
    assert filtering, "the walk found nothing — it is not looking where it thinks"

    undeclared = {
        name
        for name, statement in filtering.items()
        if "users" in sql(statement).lower() and statement not in USER_STATEMENTS
    }
    assert not undeclared, f"not declared in USER_STATEMENTS: {sorted(undeclared)}"


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
