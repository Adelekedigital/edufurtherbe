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
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Delete, Select, Update
from sqlalchemy.dialects import postgresql

from app.domain.enums import ApprovalStatus, MentorStatusType
from app.infra.db import asset_store, provisioning_store

PREDICATE = "deleted_at IS NULL"

#: Repo root, for the cross-artefact check at the end of this file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


# --------------------------------------------------------------------------
# Which mentors get a status event — one rule, two writers
# --------------------------------------------------------------------------

#: Approval states that record no event at all. The migration backfill spells
#: this out twice as ``WHERE approval_status <> 'pending'``; the ETL skips the
#: same mentors. Two writers of one table, and nothing in the type system links
#: them — which is the whole reason this section exists.
NO_HISTORY = frozenset({ApprovalStatus.PENDING})


def test_every_other_approval_status_is_a_real_status_type() -> None:
    """**The crossing that broke the loader, generalised.**

    ``ApprovalStatus.value`` is handed to a ``mentor_status_type`` parameter as a
    string, through raw SQL. mypy cannot see that the two enums are different, so
    ``pending`` reached the database and took a single-transaction load down with
    it — for the *default* state, which most of the export is in.

    Asserted over the whole enum rather than the one value that bit us: a status
    added later crosses the same gap, and fails here rather than at a cutover.
    """
    labels = {member.value for member in MentorStatusType}
    assert labels, "no status labels; this test would assert nothing"

    unmapped = sorted(
        status.value
        for status in ApprovalStatus
        if status not in NO_HISTORY and status.value not in labels
    )

    assert not unmapped, (
        f"{unmapped} would be written into `mentor_status_type`, which holds "
        f"{sorted(labels)}. Either map it, or add it to NO_HISTORY."
    )


def test_the_migration_excludes_the_same_mentors_the_etl_does() -> None:
    """The two writers of ``mentor_status_events`` must agree on who has none.

    A per-event filter — seeding only values the enum happens to accept — looks
    equivalent and diverges here: a pending mentor's ``listing_status`` is
    ``unlisted``, which *is* a member, so that version seeds a listing event the
    backfill does not, and a mentor's history depends on whether they arrived by
    migration or by ETL.

    Matched on the migration that **writes** the table, not one that merely names
    it: an earlier revision mentions it in prose, and matching the mention picked
    that file and failed for the wrong reason.
    """
    backfills = [
        text
        for text in (
            path.read_text(encoding="utf-8")
            for path in (PROJECT_ROOT / "migrations" / "versions").glob("*.py")
        )
        if "INSERT INTO mentor_status_events" in text
    ]
    assert backfills, "no migration seeds this table; this test would assert nothing"

    backfill = "\n".join(backfills)
    for status in NO_HISTORY:
        assert f"approval_status <> '{status.value}'" in backfill, (
            f"the ETL skips {status.value} mentors and the backfill does not"
        )
