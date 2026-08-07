"""The provisioning CLI, against a real database and a fake Supabase.

Both doubles come from ``tests/conftest.py`` — one stateful ``FakeSupabase`` and
one representation of GoTrue's paging, shared with the unit suite. Three private
copies of that knowledge is what let the stranded-user defect through a green
run of this file.

The Admin API adapter is the real one, driven over ``httpx.MockTransport`` — a
hand-written stand-in for ``SupabaseAdminClient`` would test the fake.
"""

from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.enums import AdminRole, PrimaryRole
from app.domain.provisioning import Action, Candidate, Plan
from app.infra.db.provisioning_store import ProvisioningStore

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: The `fake_supabase` fixture hands back the double's *class*, so a test can
#: configure it. `tests/` is not a package — importing the class for a precise
#: annotation would mean adding `__init__.py` and changing import semantics for
#: every module in the suite, which conftest.py explains is the larger change.
type FakeSupabaseFactory = Any

# A timestamp no clock in this test can produce, so "preserved" and "rewritten"
# cannot be confused. This stands in for Bubble's Modified Date.
MIGRATED_AT = datetime(2023, 12, 7, 18, 36, 46, 179000, tzinfo=UTC)


async def seed(engine: AsyncEngine, email: str, *, auth_id: UUID | None = None) -> UUID:
    """One user, carrying a migrated ``updated_at``."""
    async with engine.begin() as connection:
        return (
            await connection.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone, "
                    "created_at, updated_at, auth_id) "
                    "VALUES (:email, 'Ada', 'mentee', 'Africa/Lagos', :at, :at, :auth_id) "
                    "RETURNING id"
                ),
                {"email": email, "at": MIGRATED_AT, "auth_id": auth_id},
            )
        ).scalar_one()


async def read(engine: AsyncEngine, user_id: UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT auth_id, updated_at, email FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# --------------------------------------------------------------------------
# --link-migrated
# --------------------------------------------------------------------------


async def test_a_migrated_user_gets_an_account(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    supabase = fake_supabase()
    user_id = await seed(db_engine, "ada@example.com")

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    assert outcome.created == 1
    assert supabase.creates == ["ada@example.com"]
    assert (await read(db_engine, user_id))["auth_id"] == supabase.accounts["ada@example.com"]


async def test_provisioning_does_not_rewrite_the_migrated_modified_date(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """``updated_at`` carries Bubble's Modified Date, which M1c loaded on purpose.

    ``trg_set_updated_at`` fires on any ``UPDATE``, so without the disable in
    ``apply`` this run silently replaces 1,200 modification dates with the moment
    provisioning happened — a day after the migration that preserved them, and
    with nothing to restore them from.
    """
    user_id = await seed(db_engine, "ada@example.com")

    await provision_script.link_migrated(store, fake_supabase().client(), dry_run=False)

    assert (await read(db_engine, user_id))["updated_at"] == MIGRATED_AT


async def test_the_trigger_is_left_enabled_afterwards(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """A disable that leaks would stop ``updated_at`` maintaining itself for every
    ordinary write the application makes afterwards — silently, and forever."""
    user_id = await seed(db_engine, "ada@example.com")

    await provision_script.link_migrated(store, fake_supabase().client(), dry_run=False)

    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET first_name = 'Grace' WHERE id = :id"), {"id": user_id}
        )

    assert (await read(db_engine, user_id))["updated_at"] > MIGRATED_AT


async def test_an_account_that_already_exists_is_linked_rather_than_recreated(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """The resume path: the previous run created the account and died before
    writing ``auth_id``. Creating a second one would leave an unreachable
    account and a user who cannot log in."""
    supabase = fake_supabase()
    existing = uuid4()
    supabase.accounts["ada@example.com"] = existing
    user_id = await seed(db_engine, "ada@example.com")

    await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    assert supabase.creates == []
    assert (await read(db_engine, user_id))["auth_id"] == existing


async def test_an_address_that_is_a_substring_of_another_is_still_found(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """The defect this whole PR exists for.

    Supabase's ``filter`` is a substring match returning newest-first, so a lookup
    for ``ada@x.com`` can be answered with ``xada@x.com``. Asking for a single row
    meant the real account was never on the page: the exact-match comparison
    correctly rejected the neighbour, the planner chose CREATE, Supabase answered
    ``email_exists``, the retry lookup returned nothing again — and the run raised.
    **Every subsequent re-run failed identically**, so the documented recovery plan
    could not recover the user.

    Nothing in the suite caught it because the fake ignored ``per_page``.
    """
    supabase = fake_supabase()
    wanted = uuid4()
    # Insertion order is the fake's stand-in for age, so the neighbour is *newer*
    # and comes back first — which is the arrangement that breaks a one-row page.
    supabase.accounts["ada@x.com"] = wanted
    supabase.accounts["xada@x.com"] = uuid4()
    user_id = await seed(db_engine, "ada@x.com")

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    assert outcome.failed == ()
    assert outcome.linked == 1
    assert supabase.creates == []
    assert (await read(db_engine, user_id))["auth_id"] == wanted


async def test_a_user_soft_deleted_mid_run_is_not_given_a_live_account(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
) -> None:
    """The candidate list is read once and the run then takes minutes to hours.

    A user soft-deleted while it is in flight would otherwise receive a live
    ``auth_id`` from a plan built when they were live — the guard belongs on the
    ``UPDATE`` as well as on the read, per non-negotiable #5.
    """
    user_id = await seed(db_engine, "ada@example.com")
    stale = Plan(
        candidate=Candidate(user_id=user_id, email="ada@example.com", auth_id=None),
        action=Action.CREATE,
    )
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    assert await store.link(stale.candidate.user_id, uuid4()) is False
    assert (await read(db_engine, user_id))["auth_id"] is None


async def test_an_account_the_row_will_not_take_is_reported_rather_than_dropped(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A created account that cannot be recorded is unreferenced, and silence about
    it makes the four counters stop summing to the total the run printed.

    ``apply`` is forced to refuse rather than raced into refusing. The interleaving
    that produces this in production — a row soft-deleted or linked between the
    candidate read and the write — cannot be staged from inside a single
    ``link_migrated`` call, and the property under test is the *accounting*, not
    the cause. ``test_a_user_soft_deleted_mid_run_is_not_given_a_live_account``
    covers the cause against the real database.

    An earlier version of this test asserted the sum without ever reaching the
    branch: ``apply`` succeeded for its only live candidate, so deleting the
    failure record left it green. The mutation batch is what said so.
    """
    supabase = fake_supabase()
    await seed(db_engine, "ada@example.com")

    async def refuses(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(store, "link", refuses)

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    orphan = supabase.accounts["ada@example.com"]
    assert outcome.created == 0
    assert len(outcome.failed) == 1
    # The id, because recovering an orphaned account starts by knowing which one.
    assert str(orphan) in outcome.failed[0]
    assert outcome.created + outcome.linked + outcome.skipped + len(outcome.failed) == 1


async def test_a_second_run_costs_nothing(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """Re-running is the recovery plan, so it has to be cheap as well as safe:
    1,200 already-provisioned users must not spend 1,200 API calls."""
    supabase = fake_supabase()
    user_id = await seed(db_engine, "ada@example.com")

    await provision_script.link_migrated(store, supabase.client(), dry_run=False)
    first = await read(db_engine, user_id)

    supabase.creates.clear()
    supabase.lookups.clear()
    await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    assert supabase.creates == []
    assert supabase.lookups == []
    assert await read(db_engine, user_id) == first


async def test_one_failing_user_does_not_end_the_run(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """A run that stops at the first failure leaves the rest of the cutover
    undone, and the operator with no idea how much was."""
    supabase = fake_supabase(fail_for="broken@example.com")
    broken = await seed(db_engine, "broken@example.com")
    fine = await seed(db_engine, "ada@example.com")

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    # 2, not 1: work was done and something needs a human — the same distinction
    # `load_identity.py` makes.
    assert provision_script.exit_code(outcome) == 2
    assert [f for f in outcome.failed if "broken@example.com" in f]
    assert outcome.created == 1

    assert (await read(db_engine, broken))["auth_id"] is None
    assert (await read(db_engine, fine))["auth_id"] is not None


async def test_a_row_linked_by_another_run_is_not_overwritten(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
) -> None:
    """Two operators, one runbook, one freeze.

    The plan here was built from a read that happened before the other run
    committed, so it still says "unlinked". Without ``AND auth_id IS NULL`` on the
    ``UPDATE``, this write replaces a working Supabase identifier with a second
    one — leaving an account nothing references and a user who cannot log in.
    """
    first = uuid4()
    user_id = await seed(db_engine, "ada@example.com", auth_id=first)
    stale = Plan(
        candidate=Candidate(user_id=user_id, email="ada@example.com", auth_id=None),
        action=Action.LINK,
        existing_auth_id=uuid4(),
    )

    assert await store.link(stale.candidate.user_id, stale.existing_auth_id) is False
    assert (await read(db_engine, user_id))["auth_id"] == first


async def test_a_soft_deleted_user_is_never_provisioned(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    supabase = fake_supabase()
    user_id = await seed(db_engine, "gone@example.com")
    await seed(db_engine, "ada@example.com")
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    # Both halves. `creates == []` alone would pass against a run that did nothing,
    # so the live user proves the run was working when it skipped the deleted one.
    assert supabase.creates == ["ada@example.com"]
    assert outcome.created == 1


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


async def test_a_dry_run_creates_nothing_anywhere(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """The check an operator runs before the real one. If it writes, it is not a
    check — and it is run against production Supabase by definition."""
    supabase = fake_supabase()
    user_id = await seed(db_engine, "ada@example.com")

    outcome = await provision_script.link_migrated(store, supabase.client(), dry_run=True)

    # The positive half. Without it this test passes identically against a dry
    # run that planned nothing at all, which is the failure it exists to catch.
    assert outcome.created == 1
    assert supabase.creates == []
    assert supabase.accounts == {}
    assert (await read(db_engine, user_id))["auth_id"] is None


# --------------------------------------------------------------------------
# --verify
# --------------------------------------------------------------------------


async def test_verify_catches_an_auth_id_supabase_does_not_have(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """ADR 0014 names this as its weakest point, and ADR 0018 §3 closes the half of
    it that points from us to the provider. A user whose Supabase account was
    deleted looks entirely normal here and cannot log in; without this the first
    report is a support ticket."""
    supabase = fake_supabase()
    await seed(db_engine, "ghost@example.com", auth_id=uuid4())

    assert await provision_script.verify(store, supabase.client()) == 2


async def test_verify_passes_when_both_sides_agree(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    supabase = fake_supabase()
    await seed(db_engine, "ada@example.com")
    await provision_script.link_migrated(store, supabase.client(), dry_run=False)

    assert await provision_script.verify(store, supabase.client()) == 0


async def test_verify_reports_an_address_the_two_systems_disagree_on(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """Not fatal, and worth knowing: a password reset then goes to an address we do
    not show, and nothing else in the system compares the two."""
    supabase = fake_supabase()
    auth_id = uuid4()
    supabase.accounts["elsewhere@example.com"] = auth_id
    await seed(db_engine, "ada@example.com", auth_id=auth_id)

    assert await provision_script.verify(store, supabase.client()) == 2


# --------------------------------------------------------------------------
# --create
# --------------------------------------------------------------------------


async def test_create_makes_an_account_and_a_row(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    supabase = fake_supabase()

    assert (
        await provision_script.create(
            store,
            supabase.client(),
            email="new@example.com",
            role=PrimaryRole.MENTOR,
            name="Grace",
            dry_run=False,
        )
        == 0
    )

    async with db_engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT primary_role, first_name, auth_id FROM users WHERE email = :e"),
                    {"e": "new@example.com"},
                )
            )
            .mappings()
            .one()
        )
    assert row["primary_role"] == "mentor"
    assert row["first_name"] == "Grace"
    assert row["auth_id"] == supabase.accounts["new@example.com"]


async def test_create_refuses_an_address_that_already_exists(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """Otherwise the unique index refuses *after* the Supabase account is made,
    leaving an orphan nobody knows about."""
    supabase = fake_supabase()
    await seed(db_engine, "ada@example.com")

    assert (
        await provision_script.create(
            store,
            supabase.client(),
            email="ada@example.com",
            role=PrimaryRole.MENTEE,
            name=None,
            dry_run=False,
        )
        == 1
    )
    assert supabase.creates == []


async def test_create_reports_an_orphaned_account_when_the_row_cannot_be_written(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
    fake_supabase: FakeSupabaseFactory,
) -> None:
    """``users.auth_id`` is plain UNIQUE, not partial, so a **soft-deleted** row
    still holds its Supabase account forever.

    Creating for that same address finds the existing account and then collides on
    the insert. The account is real and now referenced by nothing; exiting 1 would
    tell the operator "refused, nothing done", which is the one thing that is false.
    """
    supabase = fake_supabase()
    held = uuid4()
    supabase.accounts["ada@example.com"] = held
    user_id = await seed(db_engine, "ada@example.com", auth_id=held)
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    assert (
        await provision_script.create(
            store,
            supabase.client(),
            email="ada@example.com",
            role=PrimaryRole.MENTEE,
            name=None,
            dry_run=False,
        )
        == 2
    )


# --------------------------------------------------------------------------
# --grant-admin
# --------------------------------------------------------------------------


async def granted(engine: AsyncEngine, email: str) -> int:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT count(*) FROM admin_users a JOIN users u ON u.id = a.user_id "
                    "WHERE u.email = :e AND a.revoked_at IS NULL"
                ),
                {"e": email},
            )
        ).scalar_one()


async def test_a_grant_is_recorded_with_no_granter(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
) -> None:
    """Null ``granted_by`` is the honest record of a grant made out of band. A
    synthetic actor would look like knowledge we do not have."""
    await seed(db_engine, "ada@example.com")

    assert (
        await provision_script.grant_admin(
            store,
            email="ada@example.com",
            role=AdminRole.SUPER_ADMIN,
            dry_run=False,
        )
        == 0
    )

    async with db_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT granted_by FROM admin_users"))
        ).scalar_one() is None
    assert await granted(db_engine, "ada@example.com") == 1


async def test_granting_the_same_role_twice_is_a_no_op(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
) -> None:
    """The partial unique index would otherwise refuse, and a runbook step that
    fails on its second run is a step people stop running."""
    await seed(db_engine, "ada@example.com")

    for _ in range(2):
        assert (
            await provision_script.grant_admin(
                store,
                email="ada@example.com",
                role=AdminRole.SUPER_ADMIN,
                dry_run=False,
            )
            == 0
        )

    assert await granted(db_engine, "ada@example.com") == 1


async def test_a_revoked_role_can_be_granted_again(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
) -> None:
    """The audit row stays and does not block the re-grant — which is what stops
    an audit trail becoming something people delete rows from."""
    await seed(db_engine, "ada@example.com")
    await provision_script.grant_admin(
        store, email="ada@example.com", role=AdminRole.SUPER_ADMIN, dry_run=False
    )
    async with db_engine.begin() as connection:
        await connection.execute(text("UPDATE admin_users SET revoked_at = now()"))

    await provision_script.grant_admin(
        store, email="ada@example.com", role=AdminRole.SUPER_ADMIN, dry_run=False
    )

    assert await granted(db_engine, "ada@example.com") == 1
    async with db_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM admin_users"))
        ).scalar_one() == 2


async def test_granting_to_an_unknown_address_refuses(
    store: ProvisioningStore,
    provision_script: ModuleType,
) -> None:

    assert (
        await provision_script.grant_admin(
            store, email="nobody@example.com", role=AdminRole.SUPER_ADMIN, dry_run=False
        )
        == 1
    )


async def test_a_dry_run_grants_nothing(
    db_engine: AsyncEngine,
    store: ProvisioningStore,
    provision_script: ModuleType,
) -> None:
    await seed(db_engine, "ada@example.com")

    assert (
        await provision_script.grant_admin(
            store, email="ada@example.com", role=AdminRole.SUPER_ADMIN, dry_run=True
        )
        == 0
    )

    assert await granted(db_engine, "ada@example.com") == 0
