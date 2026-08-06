"""The provisioning CLI, against a real database and a fake Supabase.

The fake is stateful rather than a stub sequence, because the properties worth
testing here are all about what a *second* run does: a re-run must cost nothing,
find what the first run created, and never make a second account for the same
address. A canned response cannot express any of that.

The Admin API adapter is the real one, driven over ``httpx.MockTransport`` — a
hand-written stand-in for ``SupabaseAdminClient`` would test the fake.
"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.auth.admin import SupabaseAdminClient

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE = "https://project.supabase.co"

# A timestamp no clock in this test can produce, so "preserved" and "rewritten"
# cannot be confused. This stands in for Bubble's Modified Date.
MIGRATED_AT = datetime(2023, 12, 7, 18, 36, 46, 179000, tzinfo=UTC)


def load_script() -> ModuleType:
    """Import scripts/provision_auth.py, which is a script rather than a module."""
    spec = importlib.util.spec_from_file_location(
        "provision_auth", PROJECT_ROOT / "scripts" / "provision_auth.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSupabase:
    """Enough of the Admin API to be re-run against.

    ``fail_for`` makes one address raise, which is how the "a bad user does not
    end the run" property is tested — the alternative is waiting for a real one.
    """

    def __init__(self, *, fail_for: str | None = None) -> None:
        self.accounts: dict[str, UUID] = {}
        self.creates: list[str] = []
        self.lookups: list[str] = []
        self.fail_for = fail_for

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return self._create(request)
        # `GET /admin/users` is a search; `GET /admin/users/{id}` is a fetch.
        # The adapter uses both, so the fake has to tell them apart.
        tail = request.url.path.rsplit("/", 1)[-1]
        return self._find(request) if tail == "users" else self._get(UUID(tail))

    def _create(self, request: httpx.Request) -> httpx.Response:
        email = json.loads(request.content)["email"]
        self.creates.append(email)
        if email == self.fail_for:
            return httpx.Response(500, json={"msg": "boom"})
        if email in self.accounts:
            return httpx.Response(422, json={"error_code": "email_exists"})
        self.accounts[email] = uuid4()
        return httpx.Response(200, json={"id": str(self.accounts[email]), "email": email})

    def _find(self, request: httpx.Request) -> httpx.Response:
        """A substring search, newest-first, honouring ``page`` and ``per_page``.

        **The paging is the point.** The first version of this fake returned every
        substring match and ignored ``per_page`` entirely, so it could not test
        the client's decision about how many rows to ask for — and a client asking
        for one row could never find an address that is a substring of a
        newer one. Both this suite and a 43-user run against the real export were
        green over a defect that permanently stranded such a user.
        """
        needle = request.url.params.get("filter", "")
        self.lookups.append(needle)
        if needle == self.fail_for:
            return httpx.Response(500, json={"msg": "boom"})

        # GoTrue orders newest-first; insertion order reversed stands in for that.
        matches = [
            {"id": str(identifier), "email": address}
            for address, identifier in reversed(list(self.accounts.items()))
            if needle in address
        ]
        page = int(request.url.params.get("page", 1))
        per_page = int(request.url.params.get("per_page", 50))
        start = (page - 1) * per_page
        return httpx.Response(200, json={"users": matches[start : start + per_page]})

    def _get(self, identifier: UUID) -> httpx.Response:
        for address, known in self.accounts.items():
            if known == identifier:
                return httpx.Response(200, json={"id": str(identifier), "email": address})
        return httpx.Response(404)

    def client(self) -> SupabaseAdminClient:
        return SupabaseAdminClient(
            base_url=BASE,
            service_role_key="test-key",
            client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            sleep=lambda _: None,
        )


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


async def test_a_migrated_user_gets_an_account(db_engine: AsyncEngine) -> None:
    script = load_script()
    supabase = FakeSupabase()
    user_id = await seed(db_engine, "ada@example.com")

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    assert outcome.created == 1
    assert supabase.creates == ["ada@example.com"]
    assert (await read(db_engine, user_id))["auth_id"] == supabase.accounts["ada@example.com"]


async def test_provisioning_does_not_rewrite_the_migrated_modified_date(
    db_engine: AsyncEngine,
) -> None:
    """``updated_at`` carries Bubble's Modified Date, which M1c loaded on purpose.

    ``trg_set_updated_at`` fires on any ``UPDATE``, so without the disable in
    ``apply`` this run silently replaces 1,200 modification dates with the moment
    provisioning happened — a day after the migration that preserved them, and
    with nothing to restore them from.
    """
    script = load_script()
    user_id = await seed(db_engine, "ada@example.com")

    await script.link_migrated(db_engine, FakeSupabase().client(), dry_run=False)

    assert (await read(db_engine, user_id))["updated_at"] == MIGRATED_AT


async def test_the_trigger_is_left_enabled_afterwards(db_engine: AsyncEngine) -> None:
    """A disable that leaks would stop ``updated_at`` maintaining itself for every
    ordinary write the application makes afterwards — silently, and forever."""
    script = load_script()
    user_id = await seed(db_engine, "ada@example.com")

    await script.link_migrated(db_engine, FakeSupabase().client(), dry_run=False)

    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET first_name = 'Grace' WHERE id = :id"), {"id": user_id}
        )

    assert (await read(db_engine, user_id))["updated_at"] > MIGRATED_AT


async def test_an_account_that_already_exists_is_linked_rather_than_recreated(
    db_engine: AsyncEngine,
) -> None:
    """The resume path: the previous run created the account and died before
    writing ``auth_id``. Creating a second one would leave an unreachable
    account and a user who cannot log in."""
    script = load_script()
    supabase = FakeSupabase()
    existing = uuid4()
    supabase.accounts["ada@example.com"] = existing
    user_id = await seed(db_engine, "ada@example.com")

    await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    assert supabase.creates == []
    assert (await read(db_engine, user_id))["auth_id"] == existing


async def test_an_address_that_is_a_substring_of_another_is_still_found(
    db_engine: AsyncEngine,
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
    script = load_script()
    supabase = FakeSupabase()
    wanted = uuid4()
    # Insertion order is the fake's stand-in for age, so the neighbour is *newer*
    # and comes back first — which is the arrangement that breaks a one-row page.
    supabase.accounts["ada@x.com"] = wanted
    supabase.accounts["xada@x.com"] = uuid4()
    user_id = await seed(db_engine, "ada@x.com")

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    assert outcome.failed == ()
    assert outcome.linked == 1
    assert supabase.creates == []
    assert (await read(db_engine, user_id))["auth_id"] == wanted


async def test_a_user_soft_deleted_mid_run_is_not_given_a_live_account(
    db_engine: AsyncEngine,
) -> None:
    """The candidate list is read once and the run then takes minutes to hours.

    A user soft-deleted while it is in flight would otherwise receive a live
    ``auth_id`` from a plan built when they were live — the guard belongs on the
    ``UPDATE`` as well as on the read, per non-negotiable #5.
    """
    script = load_script()
    user_id = await seed(db_engine, "ada@example.com")
    stale = script.Plan(
        candidate=script.Candidate(user_id=user_id, email="ada@example.com", auth_id=None),
        action=script.Action.CREATE,
    )
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    assert await script.apply(db_engine, stale, uuid4()) is False
    assert (await read(db_engine, user_id))["auth_id"] is None


async def test_an_account_the_row_will_not_take_is_reported_rather_than_dropped(
    db_engine: AsyncEngine,
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
    script = load_script()
    supabase = FakeSupabase()
    await seed(db_engine, "ada@example.com")

    async def refuses(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(script, "apply", refuses)

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    orphan = supabase.accounts["ada@example.com"]
    assert outcome.created == 0
    assert len(outcome.failed) == 1
    # The id, because recovering an orphaned account starts by knowing which one.
    assert str(orphan) in outcome.failed[0]
    assert outcome.created + outcome.linked + outcome.skipped + len(outcome.failed) == 1


async def test_a_second_run_costs_nothing(db_engine: AsyncEngine) -> None:
    """Re-running is the recovery plan, so it has to be cheap as well as safe:
    1,200 already-provisioned users must not spend 1,200 API calls."""
    script = load_script()
    supabase = FakeSupabase()
    user_id = await seed(db_engine, "ada@example.com")

    await script.link_migrated(db_engine, supabase.client(), dry_run=False)
    first = await read(db_engine, user_id)

    supabase.creates.clear()
    supabase.lookups.clear()
    await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    assert supabase.creates == []
    assert supabase.lookups == []
    assert await read(db_engine, user_id) == first


async def test_one_failing_user_does_not_end_the_run(db_engine: AsyncEngine) -> None:
    """A run that stops at the first failure leaves the rest of the cutover
    undone, and the operator with no idea how much was."""
    script = load_script()
    supabase = FakeSupabase(fail_for="broken@example.com")
    broken = await seed(db_engine, "broken@example.com")
    fine = await seed(db_engine, "ada@example.com")

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    # 2, not 1: work was done and something needs a human — the same distinction
    # `load_identity.py` makes.
    assert script.exit_code(outcome) == 2
    assert [f for f in outcome.failed if "broken@example.com" in f]
    assert outcome.created == 1

    assert (await read(db_engine, broken))["auth_id"] is None
    assert (await read(db_engine, fine))["auth_id"] is not None


async def test_a_row_linked_by_another_run_is_not_overwritten(db_engine: AsyncEngine) -> None:
    """Two operators, one runbook, one freeze.

    The plan here was built from a read that happened before the other run
    committed, so it still says "unlinked". Without ``AND auth_id IS NULL`` on the
    ``UPDATE``, this write replaces a working Supabase identifier with a second
    one — leaving an account nothing references and a user who cannot log in.
    """
    script = load_script()
    first = uuid4()
    user_id = await seed(db_engine, "ada@example.com", auth_id=first)
    stale = script.Plan(
        candidate=script.Candidate(user_id=user_id, email="ada@example.com", auth_id=None),
        action=script.Action.LINK,
        existing_auth_id=uuid4(),
    )

    assert await script.apply(db_engine, stale, stale.existing_auth_id) is False
    assert (await read(db_engine, user_id))["auth_id"] == first


async def test_a_soft_deleted_user_is_never_provisioned(db_engine: AsyncEngine) -> None:
    script = load_script()
    supabase = FakeSupabase()
    user_id = await seed(db_engine, "gone@example.com")
    await seed(db_engine, "ada@example.com")
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    # Both halves. `creates == []` alone would pass against a run that did nothing,
    # so the live user proves the run was working when it skipped the deleted one.
    assert supabase.creates == ["ada@example.com"]
    assert outcome.created == 1


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


async def test_a_dry_run_creates_nothing_anywhere(db_engine: AsyncEngine) -> None:
    """The check an operator runs before the real one. If it writes, it is not a
    check — and it is run against production Supabase by definition."""
    script = load_script()
    supabase = FakeSupabase()
    user_id = await seed(db_engine, "ada@example.com")

    outcome = await script.link_migrated(db_engine, supabase.client(), dry_run=True)

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
) -> None:
    """ADR 0014 names this as its weakest point. A user whose Supabase account was
    deleted looks entirely normal here and cannot log in; without this the first
    report is a support ticket."""
    script = load_script()
    supabase = FakeSupabase()
    await seed(db_engine, "ghost@example.com", auth_id=uuid4())

    assert await script.verify(db_engine, supabase.client()) == 2


async def test_verify_passes_when_both_sides_agree(db_engine: AsyncEngine) -> None:
    script = load_script()
    supabase = FakeSupabase()
    await seed(db_engine, "ada@example.com")
    await script.link_migrated(db_engine, supabase.client(), dry_run=False)

    assert await script.verify(db_engine, supabase.client()) == 0


async def test_verify_reports_an_address_the_two_systems_disagree_on(
    db_engine: AsyncEngine,
) -> None:
    """Not fatal, and worth knowing: a password reset then goes to an address we do
    not show, and nothing else in the system compares the two."""
    script = load_script()
    supabase = FakeSupabase()
    auth_id = uuid4()
    supabase.accounts["elsewhere@example.com"] = auth_id
    await seed(db_engine, "ada@example.com", auth_id=auth_id)

    assert await script.verify(db_engine, supabase.client()) == 2


# --------------------------------------------------------------------------
# --create
# --------------------------------------------------------------------------


async def test_create_makes_an_account_and_a_row(db_engine: AsyncEngine) -> None:
    script = load_script()
    supabase = FakeSupabase()

    assert (
        await script.create(
            db_engine,
            supabase.client(),
            email="new@example.com",
            role=script.PrimaryRole.MENTOR,
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


async def test_create_refuses_an_address_that_already_exists(db_engine: AsyncEngine) -> None:
    """Otherwise the unique index refuses *after* the Supabase account is made,
    leaving an orphan nobody knows about."""
    script = load_script()
    supabase = FakeSupabase()
    await seed(db_engine, "ada@example.com")

    assert (
        await script.create(
            db_engine,
            supabase.client(),
            email="ada@example.com",
            role=script.PrimaryRole.MENTEE,
            name=None,
            dry_run=False,
        )
        == 1
    )
    assert supabase.creates == []


async def test_create_reports_an_orphaned_account_when_the_row_cannot_be_written(
    db_engine: AsyncEngine,
) -> None:
    """``users.auth_id`` is plain UNIQUE, not partial, so a **soft-deleted** row
    still holds its Supabase account forever.

    Creating for that same address finds the existing account and then collides on
    the insert. The account is real and now referenced by nothing; exiting 1 would
    tell the operator "refused, nothing done", which is the one thing that is false.
    """
    script = load_script()
    supabase = FakeSupabase()
    held = uuid4()
    supabase.accounts["ada@example.com"] = held
    user_id = await seed(db_engine, "ada@example.com", auth_id=held)
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_id}
        )

    assert (
        await script.create(
            db_engine,
            supabase.client(),
            email="ada@example.com",
            role=script.PrimaryRole.MENTEE,
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


async def test_a_grant_is_recorded_with_no_granter(db_engine: AsyncEngine) -> None:
    """Null ``granted_by`` is the honest record of a grant made out of band. A
    synthetic actor would look like knowledge we do not have."""
    script = load_script()
    await seed(db_engine, "ada@example.com")

    assert (
        await script.grant_admin(
            db_engine,
            email="ada@example.com",
            role=script.AdminRole.SUPER_ADMIN,
            dry_run=False,
        )
        == 0
    )

    async with db_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT granted_by FROM admin_users"))
        ).scalar_one() is None
    assert await granted(db_engine, "ada@example.com") == 1


async def test_granting_the_same_role_twice_is_a_no_op(db_engine: AsyncEngine) -> None:
    """The partial unique index would otherwise refuse, and a runbook step that
    fails on its second run is a step people stop running."""
    script = load_script()
    await seed(db_engine, "ada@example.com")

    for _ in range(2):
        assert (
            await script.grant_admin(
                db_engine,
                email="ada@example.com",
                role=script.AdminRole.SUPER_ADMIN,
                dry_run=False,
            )
            == 0
        )

    assert await granted(db_engine, "ada@example.com") == 1


async def test_a_revoked_role_can_be_granted_again(db_engine: AsyncEngine) -> None:
    """The audit row stays and does not block the re-grant — which is what stops
    an audit trail becoming something people delete rows from."""
    script = load_script()
    await seed(db_engine, "ada@example.com")
    await script.grant_admin(
        db_engine, email="ada@example.com", role=script.AdminRole.SUPER_ADMIN, dry_run=False
    )
    async with db_engine.begin() as connection:
        await connection.execute(text("UPDATE admin_users SET revoked_at = now()"))

    await script.grant_admin(
        db_engine, email="ada@example.com", role=script.AdminRole.SUPER_ADMIN, dry_run=False
    )

    assert await granted(db_engine, "ada@example.com") == 1
    async with db_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM admin_users"))
        ).scalar_one() == 2


async def test_granting_to_an_unknown_address_refuses(db_engine: AsyncEngine) -> None:
    script = load_script()

    assert (
        await script.grant_admin(
            db_engine, email="nobody@example.com", role=script.AdminRole.SUPER_ADMIN, dry_run=False
        )
        == 1
    )


async def test_a_dry_run_grants_nothing(db_engine: AsyncEngine) -> None:
    script = load_script()
    await seed(db_engine, "ada@example.com")

    assert (
        await script.grant_admin(
            db_engine, email="ada@example.com", role=script.AdminRole.SUPER_ADMIN, dry_run=True
        )
        == 0
    )

    assert await granted(db_engine, "ada@example.com") == 0
