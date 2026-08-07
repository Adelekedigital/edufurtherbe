"""The live-user predicate, asserted across every store that has one.

This file replaces the per-store check that lived in
``test_provisioning_store.py``. The point of moving it is the point of the
predicate itself: a rule with one representation needs one test, or the second
store gets a second copy of the check and eventually a second copy of the rule.

The history is worth keeping in view. ``deleted_at IS NULL`` was hand-typed into
five statements and missed on the fifth — the ``UPDATE`` — so a user soft-deleted
mid-run would have been handed a live Supabase account. No gate saw it: a
predicate inside SQL is not a symbol that ruff or mypy can bind.
"""

import inspect
from types import ModuleType

import pytest
from sqlalchemy import Delete, Select, Update
from sqlalchemy.dialects import postgresql

from app.infra.db import asset_store, provisioning_store

PREDICATE = "deleted_at IS NULL"

#: Every store, and the statements it declares as touching existing user rows.
STORES: list[tuple[ModuleType, str]] = [
    (provisioning_store, "provisioning_store"),
    (asset_store, "asset_store"),
]


def sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


@pytest.mark.parametrize(("module", "name"), STORES, ids=[name for _, name in STORES])
def test_no_declared_statement_omits_the_live_predicate(module: ModuleType, name: str) -> None:
    declared = module.USER_STATEMENTS
    assert declared, f"{name} declares no user statements — the check would be vacuous"

    for statement in declared:
        assert PREDICATE in sql(statement), f"{name}: {sql(statement)}"


@pytest.mark.parametrize(("module", "name"), STORES, ids=[name for _, name in STORES])
def test_every_statement_that_reads_users_is_declared(module: ModuleType, name: str) -> None:
    """The completeness half, and the one that keeps this honest.

    Checking only the declared tuple would pass forever against a new statement
    nobody added to it — the list would be guarding itself. This walks the module
    instead.

    ``INSERT`` is exempt: it creates the row, so there is no existing row for a
    soft-delete predicate to be about. So is any statement that never names
    ``users`` — the asset store's updates target ``user_profiles`` by ``user_id``
    and never join the parent.
    """
    statements = {
        attribute: value
        for attribute, value in inspect.getmembers(module)
        if isinstance(value, Select | Update | Delete)
    }
    assert statements, f"{name}: the walk found nothing — it is not looking where it thinks"

    undeclared = {
        attribute
        for attribute, statement in statements.items()
        if " users" in sql(statement).lower() and statement not in module.USER_STATEMENTS
    }
    assert not undeclared, f"{name}: not declared in USER_STATEMENTS: {sorted(undeclared)}"


def test_both_stores_share_one_predicate_object() -> None:
    """Not two equal expressions — the same object.

    Equality would pass against two independently written predicates that happen
    to agree today, which is exactly the state this change exists to end.
    """
    from app.infra.db.predicates import LIVE

    assert provisioning_store.LIVE is LIVE
    assert asset_store.LIVE is LIVE
