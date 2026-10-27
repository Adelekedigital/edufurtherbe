"""The admin Credits page's reads (decision 299): find a mentee, their credits
and history, the platform credit log, and the rules.

**What carries weight**: the gate (four cases per read, as every admin read),
mentors never listed as mentees, every ledger row named by the one kind table,
and the history and the log agreeing because they are one builder.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_admin_credits import a_user, grant
from tests.integration.test_api_attendance import a_confirmed_session
from tests.integration.test_api_cancellation_refunds import cancel
from tests.integration.test_api_sessions import make_session

from app.core.config import Settings
from app.domain.credit_log import ENTRY_KINDS, FILTER_KINDS
from app.domain.credits import credit_ladder
from conftest import TEST_CREDITS, add_user, api_token, bearer, fund

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTEES = "/api/v1/admin/mentees"
LOG = "/api/v1/admin/credit-log"
RULES = "/api/v1/admin/credit-rules"


def credits_of(user_id: UUID) -> str:
    return f"{MENTEES}/{user_id}/credits"


async def an_admin(engine: AsyncEngine, role: str = "limited_access") -> dict[str, str]:
    auth = uuid4()
    await a_user(engine, auth, role=role)
    return bearer(api_token(auth))


async def a_mentee(
    engine: AsyncEngine,
    email: str,
    *,
    first: str = "Amara",
    last: str = "Okafor",
    funded: int | None = None,
    deleted: bool = False,
    mentor: bool = False,
) -> UUID:
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            email.lower(),
            first_name=first,
            last_name=last,
            primary_role="mentee",
            timezone="UTC",
            deleted_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
        )
        if mentor:
            await conn.execute(
                text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
                {"u": user_id},
            )
        if funded is not None:
            await fund(conn, user_id, funded, onboard=False)
    return user_id


async def ledger_row(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    reason: str,
    delta: int,
    source: str = "admin_grant",
    session_id: UUID | None = None,
    lot_id: UUID | None = None,
) -> UUID:
    """One lot (unless given) and one ledger row, as the writers leave them."""
    async with engine.begin() as conn:
        if lot_id is None:
            lot_id = (
                await conn.execute(
                    text(
                        "INSERT INTO credit_lots (user_id, source, quantity_granted, "
                        "quantity_remaining) VALUES (:u, :s, 1, 1) RETURNING id"
                    ),
                    {"u": user_id, "s": source},
                )
            ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason, "
                "session_id) VALUES (:u, :l, :d, :r, :s)"
            ),
            {"u": user_id, "l": lot_id, "d": delta, "r": reason, "s": session_id},
        )
    return lot_id


# --------------------------------------------------------------------------
# Who may read
# --------------------------------------------------------------------------


def paths(target: UUID) -> list[str]:
    return [MENTEES, credits_of(target), LOG, RULES]


async def test_a_caller_without_a_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    target = await a_mentee(db_engine, "t-notoken@example.test")
    for path in paths(target):
        assert (await api_client.get(path)).status_code == 401, path


@pytest.mark.parametrize("revoked", [False, True])
async def test_a_caller_without_a_live_grant_gets_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, revoked: bool
) -> None:
    """404, not 403, for no grant and for a revoked one alike."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin" if revoked else None, revoked=revoked)
    target = await a_mentee(db_engine, f"t-nogrant-{revoked}@example.test")
    for path in paths(target):
        response = await api_client.get(path, headers=bearer(api_token(auth)))
        assert response.status_code == 404, path


@pytest.mark.parametrize("role", ["limited_access", "mentor_approval", "super_admin"])
async def test_every_live_grant_may_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, role: str
) -> None:
    headers = await an_admin(db_engine, role)
    target = await a_mentee(db_engine, f"t-{role}@example.test", funded=2)
    for path in paths(target):
        assert (await api_client.get(path, headers=headers)).status_code == 200, path


# --------------------------------------------------------------------------
# Finding a mentee
# --------------------------------------------------------------------------


async def search(client: httpx.AsyncClient, headers: dict[str, str], q: str | None) -> list[Any]:
    response = await client.get(MENTEES, params={} if q is None else {"q": q}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["next_cursor"] is None
    return response.json()["data"]


@pytest.mark.parametrize("q", ["AMARA.find", "zainab", "BELLO", "zainab bello", "find@exam"])
async def test_a_mentee_is_found_by_email_or_name_in_any_case(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, q: str
) -> None:
    headers = await an_admin(db_engine)
    found = await a_mentee(db_engine, "amara.find@example.test", first="Zainab", last="Bello")

    rows = await search(api_client, headers, q)

    assert [row["user_id"] for row in rows] == [str(found)]


async def test_a_full_id_finds_exactly_that_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    found = await a_mentee(db_engine, "by-id@example.test")
    await a_mentee(db_engine, f"{found}@example.test")

    rows = await search(api_client, headers, str(found))

    assert [row["user_id"] for row in rows] == [str(found)]


async def test_like_wildcards_are_literal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    await a_mentee(db_engine, "wild@example.test", first="Ngozi", last="Eze")

    assert await search(api_client, headers, "%") == []
    assert await search(api_client, headers, "ng_zi") == []


async def test_mentors_and_deleted_accounts_are_never_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The panel's "Mentors don't have credits": a user with a mentor profile
    is not a mentee here, and nor is a deleted account."""
    headers = await an_admin(db_engine)
    kept = await a_mentee(db_engine, "kept@example.test", first="Tolu", last="Ade", funded=1)
    mentor = await a_mentee(db_engine, "mentor@example.test", first="Tolu", last="Ade", mentor=True)
    gone = await a_mentee(db_engine, "gone@example.test", first="Tolu", last="Ade", deleted=True)

    rows = await search(api_client, headers, "tolu")

    assert [row["user_id"] for row in rows] == [str(kept)]
    for hidden in (mentor, gone):
        response = await api_client.get(credits_of(hidden), headers=headers)
        assert response.status_code == 404
        assert await search(api_client, headers, str(hidden)) == []
    # A mentor whose profile was deleted is a mentee again.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )
    assert [row["user_id"] for row in await search(api_client, headers, str(mentor))] == [
        str(mentor)
    ]


async def test_no_query_lists_the_latest_credit_activity_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    older = await a_mentee(db_engine, "older@example.test", funded=1)
    await a_mentee(db_engine, "never@example.test")
    newer = await a_mentee(db_engine, "newer@example.test", funded=1)
    mentor = await a_mentee(db_engine, "busy-mentor@example.test", mentor=True)
    await ledger_row(db_engine, mentor, reason="grant", delta=1)

    rows = await search(api_client, headers, None)

    assert [row["user_id"] for row in rows] == [str(newer), str(older)]


async def test_a_row_carries_the_card_credit_block(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`credits` is what `GET /me` returns for that person, field for field."""
    mentee_auth = uuid4()
    async with db_engine.begin() as conn:
        mentee = await add_user(
            conn, "card@example.test", auth_id=mentee_auth, first_name="Card", timezone="UTC"
        )
        await fund(conn, mentee, 3, onboard=False)
    headers = await an_admin(db_engine)

    (row,) = await search(api_client, headers, "card@example.test")
    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(mentee_auth)))).json()

    assert row["credits"] == me["credits"]
    assert row["credits"]["balance"] == 3
    assert set(row) == {
        "user_id",
        "first_name",
        "last_name",
        "email",
        "avatar_url",
        "cover_color",
        "credits",
    }


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


async def history_of(
    client: httpx.AsyncClient, headers: dict[str, str], user_id: UUID
) -> list[Any]:
    response = await client.get(credits_of(user_id), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["history"]


@pytest.mark.parametrize(
    ("by", "refund_reason"), [("mentor", "mentor_canceled"), ("mentee", "mentee_canceled")]
)
async def test_a_booking_and_its_refund_read_as_they_happened(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, by: str, refund_reason: str
) -> None:
    """Booked and called off through the endpoints, then read back: newest
    first, each with the balance after it, the mentor named, the cause told."""
    booking = await a_confirmed_session(
        db_engine, api_client, f"log-{by}", starts_in=dt.timedelta(hours=13)
    )
    assert (await cancel(api_client, booking, by)).status_code == 200
    headers = await an_admin(db_engine)

    history = await history_of(api_client, headers, booking["mentee_id"])

    assert [(e["kind"], e["change"], e["balance_after"]) for e in history] == [
        ("refund", 1, TEST_CREDITS),
        ("booking", -1, TEST_CREDITS - 1),
        ("opening_balance", TEST_CREDITS, TEST_CREDITS),
    ]
    refund, booked, opening = history
    assert refund["refund_reason"] == refund_reason
    assert booked["refund_reason"] is None
    for entry in (refund, booked):
        assert entry["mentor"] == {"first_name": "Ada", "last_name": "Lovelace"}
    assert opening["mentor"] is None
    assert {e["mentee"]["user_id"] for e in history} == {str(booking["mentee_id"])}
    assert all(e["admin"] is None and e["note"] is None for e in history)


@pytest.mark.parametrize(
    ("reason", "actor", "refund_reason"),
    [
        ("session_no_show_refund", None, "mentor_no_show"),
        ("request_unfulfilled", None, "request_unfulfilled"),
        ("session_cancelled_refund", "system", "canceled"),
        ("session_cancelled_refund", "admin", "canceled"),
    ],
)
async def test_every_refund_names_its_cause(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    reason: str,
    actor: str | None,
    refund_reason: str,
) -> None:
    mentor = await a_mentee(db_engine, f"m-{reason}-{actor}@example.test", mentor=True)
    mentee = await a_mentee(db_engine, f"e-{reason}-{actor}@example.test")
    session_id = await make_session(db_engine, mentor, mentee)
    if actor is not None:
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO session_events (session_id, to_status, actor_id, actor_type) "
                    "VALUES (:s, 'cancelled', :a, :t)"
                ),
                {"s": session_id, "a": mentor, "t": actor},
            )
    await ledger_row(
        db_engine, mentee, reason=reason, delta=1, source="refund", session_id=session_id
    )

    (entry,) = await history_of(api_client, await an_admin(db_engine), mentee)

    assert (entry["kind"], entry["refund_reason"]) == ("refund", refund_reason)


async def test_an_admin_grant_names_its_admin_and_note_and_its_expiry_does_not(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin_auth = uuid4()
    async with db_engine.begin() as conn:
        await add_user(
            conn, "kemi@example.test", auth_id=admin_auth, first_name="Kemi", last_name="Ade"
        )
        await conn.execute(
            text(
                "INSERT INTO admin_users (user_id, admin_role) "
                "SELECT id, 'limited_access' FROM users WHERE auth_id = :a"
            ),
            {"a": admin_auth},
        )
    mentee = await a_mentee(db_engine, "granted@example.test")
    granted = await grant(api_client, admin_auth, [mentee], quantity=2, note="Goodwill")
    assert granted.status_code == 200, granted.text
    async with db_engine.begin() as conn:
        lot = (
            await conn.execute(text("SELECT id FROM credit_lots WHERE user_id = :u"), {"u": mentee})
        ).scalar_one()
    await ledger_row(db_engine, mentee, reason="lot_expired", delta=-2, lot_id=lot)

    expired, added = await history_of(api_client, bearer(api_token(admin_auth)), mentee)

    assert (added["kind"], added["change"], added["balance_after"]) == ("admin_grant", 2, 2)
    assert added["admin"] == {"first_name": "Kemi", "last_name": "Ade"}
    assert added["note"] == "Goodwill"
    assert (expired["kind"], expired["change"], expired["balance_after"]) == ("expired", -2, 0)
    assert expired["admin"] is None and expired["note"] is None


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("monthly_free", "monthly_free"),
        ("profile_completed", "profile_completed"),
        ("referral_unlock", "referral_unlock"),
        ("opening_balance", "opening_balance"),
    ],
)
async def test_a_grant_is_named_by_where_its_lot_came_from(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, source: str, kind: str
) -> None:
    mentee = await a_mentee(db_engine, f"src-{source}@example.test")
    await ledger_row(db_engine, mentee, reason="grant", delta=1, source=source)

    (entry,) = await history_of(api_client, await an_admin(db_engine), mentee)

    assert entry["kind"] == kind


async def test_a_deleted_mentor_is_unnamed_and_the_entry_stays(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 212's rule: the row stays, the name leaves with the person."""
    mentor = await a_mentee(db_engine, "gone-mentor@example.test", mentor=True)
    mentee = await a_mentee(db_engine, "kept-mentee@example.test")
    session_id = await make_session(db_engine, mentor, mentee, status="confirmed")
    await ledger_row(db_engine, mentee, reason="session_booked", delta=-1, session_id=session_id)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    (entry,) = await history_of(api_client, await an_admin(db_engine), mentee)

    assert entry["mentor"] == {"first_name": None, "last_name": None}


# --------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------


async def log(client: httpx.AsyncClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    response = await client.get(LOG, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_each_chip_shows_only_its_kinds(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One mentee holding every kind; each chip returns exactly its own."""
    mentor = await a_mentee(db_engine, "chips-mentor@example.test", mentor=True)
    mentee = await a_mentee(db_engine, "chips@example.test")
    session_id = await make_session(db_engine, mentor, mentee, status="cancelled")
    for source in ("monthly_free", "profile_completed", "referral_unlock", "opening_balance"):
        await ledger_row(db_engine, mentee, reason="grant", delta=1, source=source)
    lot = await ledger_row(db_engine, mentee, reason="grant", delta=1, source="admin_grant")
    await ledger_row(db_engine, mentee, reason="lot_expired", delta=-1, lot_id=lot)
    await ledger_row(db_engine, mentee, reason="session_booked", delta=-1, session_id=session_id)
    await ledger_row(
        db_engine,
        mentee,
        reason="session_cancelled_refund",
        delta=1,
        source="refund",
        session_id=session_id,
    )
    headers = await an_admin(db_engine)

    for chip, kinds in FILTER_KINDS.items():
        page = await log(api_client, headers, type=chip, mentee_id=str(mentee))
        assert sorted(e["kind"] for e in page["data"]) == sorted(kinds), chip
    assert set(FILTER_KINDS["all"]) == set(ENTRY_KINDS)


async def test_an_unknown_chip_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    response = await api_client.get(
        LOG, params={"type": "everything"}, headers=await an_admin(db_engine)
    )
    assert response.status_code == 422


async def test_the_log_pages_newest_first_and_the_history_agrees(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """51 entries: a page of 50 and one more, in one order; the panel's
    history is the log's first page for that mentee, entry for entry."""
    mentee = await a_mentee(db_engine, "pages@example.test")
    for _ in range(51):
        await ledger_row(db_engine, mentee, reason="grant", delta=1)
    headers = await an_admin(db_engine)

    first = await log(api_client, headers, mentee_id=str(mentee))
    second = await log(api_client, headers, mentee_id=str(mentee), cursor=first["next_cursor"])
    panel = (await api_client.get(credits_of(mentee), headers=headers)).json()

    assert len(first["data"]) == 50 and first["next_cursor"] is not None
    assert len(second["data"]) == 1 and second["next_cursor"] is None
    balances = [e["balance_after"] for e in first["data"] + second["data"]]
    assert balances == list(range(51, 0, -1))
    assert panel["history"] == first["data"]
    assert panel["history_next_cursor"] == first["next_cursor"]


async def test_the_log_spans_every_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    one = await a_mentee(db_engine, "span-one@example.test", funded=1)
    two = await a_mentee(db_engine, "span-two@example.test", funded=2)

    page = await log(api_client, await an_admin(db_engine))

    mine = [e for e in page["data"] if e["mentee"]["user_id"] in {str(one), str(two)}]
    assert [(e["mentee"]["user_id"], e["balance_after"]) for e in mine] == [
        (str(two), 2),
        (str(one), 1),
    ]


async def test_a_bad_cursor_is_a_422(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    response = await api_client.get(
        LOG, params={"cursor": "not-a-cursor"}, headers=await an_admin(db_engine)
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


async def test_the_rules_are_the_settings_the_ladder_reads(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    ladder = credit_ladder(Settings(_env_file=None))

    response = await api_client.get(RULES, headers=await an_admin(db_engine))

    assert response.json() == {"starter": ladder.starter, "monthly": ladder.monthly}
