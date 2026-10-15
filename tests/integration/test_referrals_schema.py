"""What the two referral tables guarantee, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything
here is outside that set: two ordering CHECKs, a lowercase CHECK, a partial
index, a uniqueness rule scoped to a pair, and foreign keys whose deletion
behaviour is load-bearing because an unlock is an entitlement.

Every constraint gets a rejecting **and** an accepting case. A test that only
proves a constraint refuses garbage cannot tell a working constraint from one
that refuses everything — and two of the accepting cases here are the reason
this file exists at all:

- **An unlock may name no referral.** ~1,200 migrated users are grandfathered as
  unlocked without ever having invited anybody, so a `NOT NULL` on
  `unlocked_by_referral_id` would make the migration impossible — and every
  rejecting test would still pass.
- **Two referrers may invite the same person.** The uniqueness rule is scoped to
  the *pair*; on `invitee_email` alone the second referrer is refused, which is
  a race to invite rather than a referral programme.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LAGOS = "Africa/Lagos"
INVITED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

INSERT_REFERRAL = """
INSERT INTO referrals
    (referrer_id, code, invitee_email, invitee_user_id,
     invited_at, signed_up_at, qualified_at)
VALUES (:referrer, :code, :email, :invitee, :invited, :signed_up, :qualified)
RETURNING id
"""

INSERT_UNLOCK = """
INSERT INTO referral_unlocks (user_id, unlocked_by_referral_id, unlocked_at)
VALUES (:user, :referral, :unlocked)
RETURNING id
"""


async def make_user(conn: AsyncConnection, email: str, role: str = "mentee") -> str:
    user_id = await add_user(conn, email, primary_role=role, timezone=LAGOS)
    return str(user_id)


class Invites:
    """A referrer, and a counter so every code in one test is distinct."""

    def __init__(self, conn: AsyncConnection, referrer: str) -> None:
        self.conn = conn
        self.referrer = referrer
        self._n = 0

    async def referral(
        self,
        *,
        referrer: str | None = None,
        code: str | None = None,
        email: str | None = "invitee@example.test",
        invitee: str | None = None,
        invited: datetime = INVITED,
        signed_up: datetime | None = None,
        qualified: datetime | None = None,
    ) -> str:
        self._n += 1
        result = await self.conn.execute(
            text(INSERT_REFERRAL),
            {
                "referrer": referrer or self.referrer,
                "code": code or f"code-{self._n}",
                "email": email,
                "invitee": invitee,
                "invited": invited,
                "signed_up": signed_up,
                "qualified": qualified,
            },
        )
        return str(result.scalar_one())

    async def unlock(self, *, user: str | None = None, referral: str | None = None) -> str:
        result = await self.conn.execute(
            text(INSERT_UNLOCK),
            {
                "user": user or self.referrer,
                "referral": referral,
                "unlocked": INVITED + timedelta(days=1),
            },
        )
        return str(result.scalar_one())


@pytest_asyncio.fixture
async def invites(db_engine: AsyncEngine) -> AsyncIterator[Invites]:
    async with db_engine.begin() as conn:
        yield Invites(conn, await make_user(conn, "referrer@example.test"))


# --------------------------------------------------------------------------
# referrals — the invite itself
# --------------------------------------------------------------------------


async def test_a_well_formed_referral_is_accepted(invites: Invites) -> None:
    assert await invites.referral()


async def test_an_invite_may_carry_no_email(invites: Invites) -> None:
    """A shared link has no addressee. `invitee_email` is how an emailed invite
    is matched back; a link carries only the code."""
    assert await invites.referral(email=None)


async def test_the_code_is_unique(invites: Invites) -> None:
    """One row per invite, and the code is what an arrival is attributed by.

    Reused across rows, an arrival could not say *which* invite it answered —
    and `invitee_email` cannot stand in, because a shared link has none.
    """
    await invites.referral(code="shared")

    with pytest.raises(IntegrityError, match="code"):
        await invites.referral(code="shared", email="other@example.test")


async def test_one_invite_per_referrer_per_email(invites: Invites) -> None:
    """Inviting the same address twice is a duplicate, not a second referral."""
    await invites.referral(email="dup@example.test")

    with pytest.raises(IntegrityError, match="referrer_id"):
        await invites.referral(email="dup@example.test")


async def test_two_referrers_may_invite_the_same_person(invites: Invites) -> None:
    """**The accepting case that decides the shape of the rule.**

    Scoped to `invitee_email` alone this is refused, which turns a referral
    programme into a race to invite — and the rejecting test above passes
    either way. Only this case tells the two apart.
    """
    other = await make_user(invites.conn, "other-referrer@example.test")
    await invites.referral(email="popular@example.test")

    assert await invites.referral(referrer=other, email="popular@example.test")


async def test_the_same_referrer_may_send_two_link_invites(invites: Invites) -> None:
    """Null emails must not collide under the pair rule.

    PostgreSQL treats nulls as distinct, so this works — asserted because it is
    the behaviour the nullable column depends on, not because it is in doubt.
    """
    await invites.referral(email=None)

    assert await invites.referral(email=None)


async def test_the_invitee_email_must_be_lowercase(invites: Invites) -> None:
    """`users.email` settled this: normalise at the boundary, and let a CHECK
    fail loudly when a writer forgets. `citext` was adopted here first and
    reversed — a case-insensitive type is a second mechanism for an invariant
    the boundary already holds."""
    with pytest.raises(IntegrityError, match="lowercase"):
        await invites.referral(email="Shouty@Example.test")


async def test_nobody_may_refer_themselves(invites: Invites) -> None:
    """Self-referral is the cheapest possible farm: one address, one unlock.

    The gate opens a *recurring* grant, so the cost of missing this is not one
    credit — it is three a month, forever.
    """
    with pytest.raises(IntegrityError, match="no_self_referral"):
        await invites.referral(invitee=invites.referrer)


async def test_a_referral_may_name_a_real_invitee(invites: Invites) -> None:
    """The accepting half of the rule above."""
    invitee = await make_user(invites.conn, "arrived@example.test")

    assert await invites.referral(invitee=invitee)


async def test_qualifying_requires_having_signed_up(invites: Invites) -> None:
    """`qualified_at` is deliberately separate from `signed_up_at` — that
    separation is the abuse boundary. Qualified *without* having signed up is
    the state that separation exists to make impossible."""
    with pytest.raises(IntegrityError, match="qualified"):
        await invites.referral(qualified=INVITED + timedelta(days=2))


async def test_a_referral_may_sign_up_and_qualify(invites: Invites) -> None:
    invitee = await make_user(invites.conn, "qualified@example.test")

    assert await invites.referral(
        invitee=invitee,
        signed_up=INVITED + timedelta(days=1),
        qualified=INVITED + timedelta(days=2),
    )


async def test_a_referral_may_sign_up_without_qualifying(invites: Invites) -> None:
    """The abuse boundary in its resting state: somebody signed up and vanished."""
    invitee = await make_user(invites.conn, "vanished@example.test")

    assert await invites.referral(invitee=invitee, signed_up=INVITED + timedelta(days=1))


# --------------------------------------------------------------------------
# referral_unlocks — the once-only gate
# --------------------------------------------------------------------------


async def test_an_unlock_is_accepted(invites: Invites) -> None:
    assert await invites.unlock(referral=await invites.referral())


async def test_a_user_unlocks_once_ever(invites: Invites) -> None:
    """The gate is once-only, and granting it twice opens a floor already open.

    Structural rather than an application check: the producer is a transition
    somebody will eventually make idempotent by retrying it.
    """
    await invites.unlock(referral=await invites.referral())

    with pytest.raises(IntegrityError, match="user_id"):
        await invites.unlock(referral=await invites.referral(email="second@example.test"))


async def test_an_unlock_may_name_no_referral(invites: Invites) -> None:
    """**The accepting case the migration depends on.**

    ~1,200 users are grandfathered as unlocked at cutover and none of them ever
    invited anybody. The canonical DDL makes this column `NOT NULL`, which would
    leave two options: invent a synthetic referral per user — an invite that
    never happened — or switch off a benefit people currently have. This is the
    #82 shape: unlocked, and the reason may be absent.
    """
    assert await invites.unlock(referral=None)


async def test_an_unlock_cannot_name_a_referral_that_does_not_exist(invites: Invites) -> None:
    """Nullable is not unchecked."""
    with pytest.raises(IntegrityError, match="unlock_belongs_to_referrer"):
        await invites.unlock(referral=str(uuid.uuid4()))


async def test_an_unlock_cannot_cite_somebody_elses_invite(invites: Invites) -> None:
    """**The guard no single-column key can give.**

    On `referrals.id` alone, this row would say the caller was unlocked by an
    invite they had nothing to do with — and one invite could be cited by any
    number of users, each collecting the recurring grant from it. Same defect
    the ledger's `credit_lot_id` had, and the third time this shape is needed
    in one phase.
    """
    stranger = await make_user(invites.conn, "stranger@example.test")
    theirs = await invites.referral(referrer=stranger, email="theirs@example.test")

    with pytest.raises(IntegrityError, match="unlock_belongs_to_referrer"):
        await invites.unlock(referral=theirs)


async def test_an_unlock_may_cite_your_own_invite(invites: Invites) -> None:
    """The accepting half. A composite key with its columns transposed rejects
    the case above just as loudly and rejects this one too."""
    assert await invites.unlock(referral=await invites.referral())


# --------------------------------------------------------------------------
# Deletion policy — an unlock is an entitlement
# --------------------------------------------------------------------------


async def test_a_referrer_who_has_invited_cannot_be_hard_deleted(invites: Invites) -> None:
    """ADR 0013: restrict where the child is evidence.

    The canonical DDL says `ON DELETE CASCADE`. An unlock is what gates the
    recurring grant, and the referral is the evidence for it — deleting the
    referrer would silently remove the record of why somebody is entitled.
    """
    referrer = await make_user(invites.conn, "deletable@example.test")
    await invites.referral(referrer=referrer, email="theirs@example.test")

    with pytest.raises(IntegrityError, match="referrals"):
        await invites.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": referrer})


async def test_an_unlocked_user_cannot_be_hard_deleted(invites: Invites) -> None:
    """A holder with an unlock and nothing else — the fixture's referrer is
    already referenced by `referrals` in most tests, and this assertion must
    report on `referral_unlocks` rather than on whichever key fires first."""
    holder = await make_user(invites.conn, "unlocked@example.test")
    await invites.unlock(user=holder, referral=None)

    with pytest.raises(IntegrityError, match="referral_unlocks"):
        await invites.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": holder})


# --------------------------------------------------------------------------
# invite_links — one permanent personal link per user (owner, 2026-10-15)
# --------------------------------------------------------------------------

INSERT_LINK = "INSERT INTO invite_links (user_id, code) VALUES (:user, :code) RETURNING id"


async def _link(conn: AsyncConnection, user: str, code: str) -> str:
    result = await conn.execute(text(INSERT_LINK), {"user": user, "code": code})
    return str(result.scalar_one())


async def test_a_user_may_have_a_personal_link(invites: Invites) -> None:
    assert await _link(invites.conn, invites.referrer, "link-1")


async def test_a_new_link_is_active(invites: Invites) -> None:
    """Null `revoked_at` is active, and it is the default: no backfill needed."""
    link = await _link(invites.conn, invites.referrer, "link-1")

    assert (
        await invites.conn.scalar(
            text("SELECT revoked_at FROM invite_links WHERE id = :l"), {"l": link}
        )
    ) is None


REVOKE = "UPDATE invite_links SET revoked_at = {at} WHERE id = :l RETURNING revoked_at"


async def test_a_link_may_be_revoked_and_reactivated(invites: Invites) -> None:
    link = await _link(invites.conn, invites.referrer, "link-1")

    assert await invites.conn.scalar(text(REVOKE.format(at="now()")), {"l": link})
    assert (await invites.conn.scalar(text(REVOKE.format(at="NULL")), {"l": link})) is None


async def test_a_link_cannot_be_revoked_before_it_existed(invites: Invites) -> None:
    link = await _link(invites.conn, invites.referrer, "link-1")

    with pytest.raises(IntegrityError, match="revoked_after_created"):
        await invites.conn.execute(
            text(REVOKE.format(at="created_at - interval '1 second'")), {"l": link}
        )


async def test_a_revoked_link_keeps_its_code_from_everyone_else(invites: Invites) -> None:
    """Revoking switches the link off; it does not give the code up."""
    other = await make_user(invites.conn, "other-linker@example.test")
    link = await _link(invites.conn, invites.referrer, "kept-code")
    await invites.conn.execute(text(REVOKE.format(at="now()")), {"l": link})

    with pytest.raises(IntegrityError, match="code"):
        await _link(invites.conn, other, "kept-code")


async def test_a_user_has_at_most_one_personal_link(invites: Invites) -> None:
    """One permanent link per user: a second is a duplicate, not a second link."""
    await _link(invites.conn, invites.referrer, "link-1")

    with pytest.raises(IntegrityError, match="user_id"):
        await _link(invites.conn, invites.referrer, "link-2")


async def test_two_users_each_have_their_own_link(invites: Invites) -> None:
    """The accepting half: unique per user, not one link for the table."""
    other = await make_user(invites.conn, "other-linker@example.test")
    await _link(invites.conn, invites.referrer, "link-1")

    assert await _link(invites.conn, other, "link-2")


async def test_a_link_code_names_one_link(invites: Invites) -> None:
    """The code is what an arrival is attributed by, so it must name one owner."""
    other = await make_user(invites.conn, "other-linker@example.test")
    await _link(invites.conn, invites.referrer, "shared")

    with pytest.raises(IntegrityError, match="code"):
        await _link(invites.conn, other, "shared")


async def test_a_link_ids_itself(invites: Invites) -> None:
    """Non-negotiable 10: the id is the database's, never the caller's."""
    link = await _link(invites.conn, invites.referrer, "link-1")

    assert uuid.UUID(link).version == 7


async def test_a_user_with_a_link_cannot_be_hard_deleted(invites: Invites) -> None:
    """Restrict, like every referral table: arrivals are attributed through it."""
    holder = await make_user(invites.conn, "linked@example.test")
    await _link(invites.conn, holder, "link-1")

    with pytest.raises(IntegrityError, match="invite_links"):
        await invites.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": holder})


INSERT_RETIRED = """
INSERT INTO invite_link_retired_codes (invite_link_id, code)
VALUES (:link, :code)
RETURNING id
"""


async def _retire(conn: AsyncConnection, link: str, code: str) -> str:
    result = await conn.execute(text(INSERT_RETIRED), {"link": link, "code": code})
    return str(result.scalar_one())


async def test_a_retired_code_is_recorded_with_its_moment(invites: Invites) -> None:
    link = await _link(invites.conn, invites.referrer, "current")
    retired = await _retire(invites.conn, link, "old-handle")

    row = (
        await invites.conn.execute(
            text("SELECT created_at FROM invite_link_retired_codes WHERE id = :i"),
            {"i": retired},
        )
    ).one()
    assert uuid.UUID(retired).version == 7
    assert row.created_at is not None


async def test_a_code_may_be_retired_more_than_once(invites: Invites) -> None:
    """A handle can be picked, dropped, picked again and dropped again, by one
    owner or by another after the hold — so `code` is not unique here."""
    other = await make_user(invites.conn, "other-linker@example.test")
    mine = await _link(invites.conn, invites.referrer, "mine-now")
    theirs = await _link(invites.conn, other, "theirs-now")
    await _retire(invites.conn, mine, "popular")
    await _retire(invites.conn, mine, "popular")

    assert await _retire(invites.conn, theirs, "popular")


async def test_a_retired_code_must_belong_to_a_real_link(invites: Invites) -> None:
    with pytest.raises(IntegrityError, match="invite_link_id"):
        await _retire(invites.conn, str(uuid.uuid4()), "orphan")


async def test_a_link_with_retired_codes_cannot_be_hard_deleted(invites: Invites) -> None:
    """Restrict: the retired codes are what the hold is read from."""
    link = await _link(invites.conn, invites.referrer, "current")
    await _retire(invites.conn, link, "old-handle")

    with pytest.raises(IntegrityError, match="invite_link_retired_codes"):
        await invites.conn.execute(text("DELETE FROM invite_links WHERE id = :l"), {"l": link})


async def test_the_hold_check_has_its_index(invites: Invites) -> None:
    """`alembic check` cannot compare a `DESC` index, so its shape is asserted."""
    definition = await invites.conn.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_invite_link_retired_codes_code_retired'"
        )
    )
    assert definition is not None
    assert "(code, created_at DESC)" in definition


async def test_retired_codes_are_append_only(invites: Invites) -> None:
    """No `updated_at` and no trigger: a retirement is never edited."""
    assert await _triggers_on(invites.conn, "invite_link_retired_codes") == []


# --------------------------------------------------------------------------
# The trigger — every referral table is mutable, so each carries one
# --------------------------------------------------------------------------


async def _triggers_on(conn: AsyncConnection, table: str) -> list[str]:
    result = await conn.execute(
        text(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = :table AND NOT t.tgisinternal "
            "ORDER BY t.tgname"
        ),
        {"table": table},
    )
    return [row[0] for row in result]


async def test_every_referral_table_maintains_updated_at(invites: Invites) -> None:
    """Neither is append-only — a referral's timestamps are filled in as the
    invitee progresses, and that is an update rather than a new row."""
    assert await _triggers_on(invites.conn, "referrals") == ["trg_set_updated_at"]
    assert await _triggers_on(invites.conn, "referral_unlocks") == ["trg_set_updated_at"]
    assert await _triggers_on(invites.conn, "invite_links") == ["trg_set_updated_at"]
