"""The admin user picker's search, `GET /admin/users` (decision 310).

**What carries weight**: the gate (four cases, as every admin read), admins and
deleted accounts never listed, `q` matching each field with wildcards literal,
the role filter, and a keyset that walks every user exactly once, nameless
users included.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_admin_credit_reads import an_admin
from tests.integration.test_api_admin_credits import a_user

from app.api.schemas.admin_users import PAGE_SIZE
from app.api.schemas.common import MAX_QUERY_LENGTH, encode_cursor, encode_id_cursor
from app.infra.db.admin_users import find_users
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

USERS = "/api/v1/admin/users"


async def a_person(
    engine: AsyncEngine,
    email: str,
    *,
    first: str | None = "Amara",
    last: str | None = "Okafor",
    role: str = "mentee",
    deleted: bool = False,
) -> UUID:
    async with engine.begin() as conn:
        return await add_user(
            conn,
            email.lower(),
            first_name=first,
            last_name=last,
            primary_role=role,
            timezone="UTC",
            deleted_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
        )


async def listed(
    client: httpx.AsyncClient, headers: dict[str, str], **params: str
) -> list[dict[str, Any]]:
    """Every row on the first page; asserts there is no second."""
    response = await client.get(USERS, params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["next_cursor"] is None
    rows: list[dict[str, Any]] = response.json()["data"]
    return rows


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["user_id"] for row in rows]


# --------------------------------------------------------------------------
# Who may read (decision 70: no token, no grant, revoked grant, live grant)
# --------------------------------------------------------------------------


async def test_a_caller_without_a_token_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(USERS)).status_code == 401


@pytest.mark.parametrize("revoked", [False, True])
async def test_a_caller_without_a_live_grant_gets_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, revoked: bool
) -> None:
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin" if revoked else None, revoked=revoked)
    await a_person(db_engine, "someone@example.test")

    response = await api_client.get(USERS, headers=bearer(api_token(auth)))

    assert response.status_code == 404


@pytest.mark.parametrize("role", ["limited_access", "mentor_approval", "super_admin"])
async def test_every_live_grant_may_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, role: str
) -> None:
    headers = await an_admin(db_engine, role)
    person = await a_person(db_engine, f"seen-{role}@example.test")

    assert ids(await listed(api_client, headers)) == [str(person)]


# --------------------------------------------------------------------------
# The row and the order
# --------------------------------------------------------------------------


async def test_a_row_is_the_picker_shape_and_is_never_cached(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    mentor = await a_person(db_engine, "tolu@example.test", first="Tolu", last="Ade", role="mentor")
    nameless = await a_person(db_engine, "zz-nameless@example.test", first=None, last=None)

    response = await api_client.get(USERS, headers=headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "data": [
            {
                "user_id": str(mentor),
                "email": "tolu@example.test",
                "first_name": "Tolu",
                "last_name": "Ade",
                "role": "mentor",
            },
            {
                "user_id": str(nameless),
                "email": "zz-nameless@example.test",
                "first_name": None,
                "last_name": None,
                "role": "mentee",
            },
        ],
        "next_cursor": None,
    }


async def test_ordered_by_name_ignoring_case_and_a_nameless_user_by_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The sort key is what the picker shows: the full name, or the email when
    there is none (a blank name counts as none)."""
    headers = await an_admin(db_engine)
    zainab = await a_person(db_engine, "a-zainab@example.test", first="Zainab", last="Bello")
    ada = await a_person(db_engine, "z-ada@example.test", first="ada", last="Obi")
    by_email = await a_person(db_engine, "musa@example.test", first=None, last=None)
    blank = await a_person(db_engine, "bisi@example.test", first="  ", last=None)
    only_last = await a_person(db_engine, "x@example.test", first=None, last="Chukwu")

    rows = await listed(api_client, headers)

    assert ids(rows) == [str(u) for u in (ada, blank, only_last, by_email, zainab)]


async def test_names_differing_only_in_case_tie_and_fall_back_to_the_id(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Case is ignored whatever the database collation: a linguistic one puts
    "kemi" before "Kemi", so without the lowercasing the newer would lead."""
    headers = await an_admin(db_engine)
    upper = await a_person(db_engine, "k1@example.test", first="Kemi", last="Ojo")
    lower = await a_person(db_engine, "k2@example.test", first="kemi", last="ojo")

    assert ids(await listed(api_client, headers)) == [str(u) for u in sorted((upper, lower))]


# --------------------------------------------------------------------------
# Who is listed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("grant", ["super_admin", "mentor_approval", "limited_access"])
async def test_an_admin_is_never_listed_and_a_revoked_one_is_a_user_again(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, grant: str
) -> None:
    headers = await an_admin(db_engine, "super_admin")
    admin = await a_user(db_engine, role=grant)
    revoked = await a_user(db_engine, role=grant, revoked=True)

    assert ids(await listed(api_client, headers)) == [str(revoked)]

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE admin_users SET revoked_at = now() WHERE user_id = :u"), {"u": admin}
        )
    assert sorted(ids(await listed(api_client, headers))) == sorted([str(admin), str(revoked)])


async def test_a_deleted_account_is_never_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    kept = await a_person(db_engine, "kept@example.test")
    await a_person(db_engine, "gone@example.test", deleted=True)

    assert ids(await listed(api_client, headers)) == [str(kept)]
    assert ids(await listed(api_client, headers, q="gone")) == []


@pytest.mark.parametrize(
    ("role", "expected"), [("mentor", ["m"]), ("mentee", ["e"]), ("all", ["e", "m"])]
)
async def test_the_role_filter_reads_the_primary_role(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, role: str, expected: list[str]
) -> None:
    headers = await an_admin(db_engine)
    people = {
        "e": await a_person(db_engine, "e@example.test", first="Ebere", role="mentee"),
        "m": await a_person(db_engine, "m@example.test", first="Musa", role="mentor"),
    }

    rows = await listed(api_client, headers, role=role)

    assert ids(rows) == [str(people[key]) for key in expected]


async def test_no_role_is_everyone_and_an_unknown_role_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    await a_person(db_engine, "e@example.test", role="mentee")
    await a_person(db_engine, "m@example.test", role="mentor")

    assert len(await listed(api_client, headers)) == 2
    response = await api_client.get(USERS, params={"role": "admin"}, headers=headers)
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@pytest.mark.parametrize("q", ["AMARA.find@", "zainab", "BELLO", "zainab bello", "  ainab bel "])
async def test_q_matches_email_first_last_and_full_name_in_any_case(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, q: str
) -> None:
    headers = await an_admin(db_engine)
    found = await a_person(db_engine, "amara.find@example.test", first="Zainab", last="Bello")
    await a_person(db_engine, "other@example.test", first="Chidi", last="Eze")

    assert ids(await listed(api_client, headers, q=q)) == [str(found)]


async def test_like_wildcards_are_literal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    await a_person(db_engine, "wild@example.test", first="Ngozi", last="Eze")
    percent = await a_person(db_engine, "100%sure@example.test", first="Ola", last="Ade")

    assert await listed(api_client, headers, q="ng_zi") == []
    assert ids(await listed(api_client, headers, q="%")) == [str(percent)]
    assert await listed(api_client, headers, q="\\") == []


async def test_a_blank_q_is_no_filter(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    await a_person(db_engine, "one@example.test", first="One")
    await a_person(db_engine, "two@example.test", first="Two")

    assert len(await listed(api_client, headers, q="   ")) == 2
    assert len(await listed(api_client, headers, q="")) == 2


async def test_q_and_role_combine(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    headers = await an_admin(db_engine)
    mentor = await a_person(db_engine, "ada.m@example.test", first="Ada", role="mentor")
    await a_person(db_engine, "ada.e@example.test", first="Ada", role="mentee")

    assert ids(await listed(api_client, headers, q="ada", role="mentor")) == [str(mentor)]


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


async def test_pages_walk_every_user_once_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """45 users: 15 nameless (ordered by email), 15 named, and 15 sharing one
    name, a tie the second page boundary falls inside. Pages of 20, 20 and 5,
    in order, nothing repeated or skipped; a tie falls back to the id."""
    headers = await an_admin(db_engine)
    nameless = [
        await a_person(db_engine, f"nameless{n:02}@example.test", first=None, last=None)
        for n in range(15)
    ]
    named = [
        await a_person(db_engine, f"p{n}@example.test", first=f"Person{n:02}", last=None)
        for n in range(15)
    ]
    same = [
        await a_person(db_engine, f"same{n}@example.test", first="Same", last="Name")
        for n in range(15)
    ]

    seen: list[str] = []
    sizes: list[int] = []
    cursor: str | None = None
    for _ in range(4):  # bounded: a cursor that never advances must fail, not hang
        params = {"cursor": cursor} if cursor else {}
        response = await api_client.get(USERS, params=params, headers=headers)
        assert response.status_code == 200, response.text
        page = response.json()
        sizes.append(len(page["data"]))
        seen.extend(ids(page["data"]))
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sizes == [PAGE_SIZE, PAGE_SIZE, 5]
    assert seen == [str(u) for u in (*nameless, *named, *sorted(same))]


@pytest.mark.parametrize("kind", ["garbage", "naming-nobody", "another-kind"])
async def test_a_bad_cursor_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, kind: str
) -> None:
    """Never page one again: a token that is not ours, one naming no user, and
    one carrying a sort key as other lists' cursors do, even for a real user."""
    headers = await an_admin(db_engine)
    someone = await a_person(db_engine, "amara@example.test")
    cursor = {
        "garbage": "not-a-cursor",
        "naming-nobody": encode_id_cursor(UUID("00000000-0000-7000-8000-000000000000")),
        "another-kind": encode_cursor("amara okafor", someone),
    }[kind]

    response = await api_client.get(USERS, params={"cursor": cursor}, headers=headers)

    assert response.status_code == 422


async def test_the_cursor_carries_no_name_or_address(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The key is read back from the last row, so the URL a page-2 request logs
    holds an id, not the person's name or email."""
    headers = await an_admin(db_engine)
    last = None
    for n in range(PAGE_SIZE + 1):
        last = await a_person(db_engine, f"zed{n:02}@example.test", first=None, last=None)
    page = (await api_client.get(USERS, headers=headers)).json()

    raw = base64.urlsafe_b64decode(page["next_cursor"]).decode()

    assert "zed" not in raw
    assert raw.split(chr(0)) == ["", page["data"][-1]["user_id"]]
    rest = await api_client.get(USERS, params={"cursor": page["next_cursor"]}, headers=headers)
    assert ids(rest.json()["data"]) == [str(last)]


@pytest.mark.parametrize(
    ("length", "status"), [(MAX_QUERY_LENGTH, 200), (MAX_QUERY_LENGTH + 1, 422)]
)
async def test_q_is_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, length: int, status: int
) -> None:
    response = await api_client.get(
        USERS, params={"q": "x" * length}, headers=await an_admin(db_engine)
    )
    assert response.status_code == status


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


async def plan_of(engine: AsyncEngine, **kwargs: Any) -> str:
    """The plan of the statement `find_users` really runs, EXPLAINed with its
    own parameters."""
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "admin_users" in statement:
            captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with async_sessionmaker(engine)() as session:
            await find_users(session, limit=PAGE_SIZE, **kwargs)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    statement, parameters = captured[0]
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql(f"EXPLAIN ANALYZE {statement}", parameters)).all()
    return "\n".join(row[0] for row in rows)


async def test_the_plan_at_five_thousand_users(db_engine: AsyncEngine) -> None:
    """Recorded in decision 310: at 5,000 users the admin exclusion is one anti
    join, never a subplan per row, and the page is a bounded top-N sort."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, first_name, last_name, primary_role, timezone) "
                "SELECT 'bulk' || n || '@example.test', "
                "CASE WHEN n % 10 = 0 THEN NULL ELSE 'First' || (n % 97) END, "
                "'Last' || n, CASE WHEN n % 25 = 0 THEN 'mentor' ELSE 'mentee' END, 'UTC' "
                "FROM generate_series(1, 5000) n"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO admin_users (user_id, admin_role) "
                "SELECT id, 'limited_access' FROM users ORDER BY id LIMIT 5"
            )
        )
        await conn.execute(text("ANALYZE"))

    browse = await plan_of(db_engine, q=None, role=None, after=None)
    search = await plan_of(db_engine, q="first4", role=None, after=None)

    for plan in (browse, search):
        assert "Anti Join" in plan, plan
        assert "SubPlan" not in plan, plan
        assert "top-N heapsort" in plan, plan
