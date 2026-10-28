"""Messages: the inbox, a chat's pages, read marks and the badge (decision 301).

Access is in `test_api_messages`; these read what a connected pair sees.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.message_helpers import (
    ROOT,
    Person,
    book,
    close_account,
    pair,
    person,
    send,
    thread,
)

from app.domain.chats import ELLIPSIS, PREVIEW_LENGTH
from app.infra.db.chat_store import thread_page, unread_total

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DAY = dt.timedelta(days=1)


async def bodies(client: httpx.AsyncClient, reader: Person, other: Person, **params: Any) -> Any:
    page = (await thread(client, reader, other, **params)).json()
    return [m["body"] for m in page["items"]], page["has_more"]


async def say(client: httpx.AsyncClient, sender: Person, to: Person, *texts: str) -> list[str]:
    ids = []
    for body in texts:
        response = await send(client, sender, to, body)
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


async def mark(client: httpx.AsyncClient, reader: Person, other: Person, up_to: Any) -> Any:
    return await client.post(
        f"{ROOT}/{other.id}/read", json={"up_to": str(up_to)}, headers=reader.headers
    )


# --------------------------------------------------------------------------
# The inbox
# --------------------------------------------------------------------------


async def test_the_inbox_puts_the_newest_message_first_then_the_newest_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await person(db_engine, "inbox-me", first_name="Mo")
    mentors = [await person(db_engine, f"inbox-{n}", first_name=n, mentor="listed") for n in "ABCD"]
    for n, (mentor, booked) in enumerate(zip(mentors, (4, 3, 1, 2), strict=True)):
        await book(db_engine, mentor, me, starts_in=(n + 2) * DAY, booked_ago=booked * DAY)
    await say(api_client, me, mentors[0], "older")
    await say(api_client, mentors[1], me, "newer")

    inbox = (await api_client.get(ROOT, headers=me.headers)).json()

    assert [row["person"]["first_name"] for row in inbox["data"]] == ["B", "A", "C", "D"]
    assert inbox["next_cursor"] is None
    newest = inbox["data"][0]
    assert newest["last"]["text"] == "newer"
    assert newest["last"]["mine"] is False
    assert newest["last"]["kind"] == "text"
    assert inbox["data"][1]["last"]["mine"] is True
    assert inbox["data"][2]["last"] is None
    assert newest["next_booking"]["session_name"] == "CV review"
    assert set(newest["person"]) == {
        "id",
        "deleted",
        "first_name",
        "last_name",
        "avatar_url",
        "avatar_focus",
        "cover_color",
        "cover_art",
        "degree",
        "institution",
        "role",
        "handle",
        "paused",
    }
    assert newest["person"]["role"] == "mentor"


async def test_the_preview_is_cut_at_a_word_and_a_deleted_message_shows_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "preview")
    long = "word " * 60
    (message,) = await say(api_client, mentee, mentor, long)

    last = (await api_client.get(ROOT, headers=mentor.headers)).json()["data"][0]["last"]
    assert last["text"].endswith(ELLIPSIS)
    assert len(last["text"]) <= PREVIEW_LENGTH
    assert last["deleted"] is False

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE messages SET deleted_at = now() WHERE id = :m"), {"m": message}
        )
    gone = (await api_client.get(ROOT, headers=mentor.headers)).json()["data"][0]
    page = (await thread(api_client, mentor, mentee)).json()

    assert gone["last"]["deleted"] is True
    assert gone["last"]["text"] is None
    assert gone["unread"] == 0
    assert page["items"][0]["deleted"] is True
    assert page["items"][0]["body"] is None


async def test_a_mentor_s_handle_shows_only_while_their_profile_is_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "handle")

    as_mentee = (await thread(api_client, mentee, mentor)).json()["person"]
    as_mentor = (await thread(api_client, mentor, mentee)).json()["person"]

    assert as_mentee["handle"] == "handle-ada"
    assert as_mentee["role"] == "mentor"
    assert as_mentor["handle"] is None
    assert as_mentor["role"] == "mentee"


async def test_the_role_follows_the_newest_booking_until_the_chat_begins(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor may book another mentor, so two people can be connected both
    ways. Before a chat exists the newest booking names the roles; the chat
    then keeps the roles it began with."""
    one = await person(db_engine, "both-one", mentor="listed")
    two = await person(db_engine, "both-two", mentor="listed")
    await book(db_engine, one, two, starts_in=2 * DAY, booked_ago=3 * DAY)
    await book(db_engine, two, one, starts_in=4 * DAY, booked_ago=DAY)

    before = (await thread(api_client, one, two)).json()["person"]["role"]
    await say(api_client, one, two, "Hi")
    await book(db_engine, one, two, starts_in=6 * DAY, booked_ago=dt.timedelta(hours=1))
    after = (await thread(api_client, one, two)).json()["person"]["role"]

    assert before == "mentor"
    assert after == "mentor"
    assert len((await api_client.get(ROOT, headers=one.headers)).json()["data"]) == 1


# --------------------------------------------------------------------------
# Unread and read marks
# --------------------------------------------------------------------------


async def test_unread_counts_their_messages_and_the_badge_is_the_sum(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await person(db_engine, "unread-me")
    first = await person(db_engine, "unread-1", mentor="listed")
    second = await person(db_engine, "unread-2", mentor="listed")
    await book(db_engine, first, me, starts_in=2 * DAY)
    await book(db_engine, second, me, starts_in=3 * DAY)
    await say(api_client, first, me, "a", "b")
    await say(api_client, second, me, "c")
    await say(api_client, me, first, "mine does not count")

    inbox = (await api_client.get(ROOT, headers=me.headers)).json()
    badge = (await api_client.get(f"{ROOT}/unread", headers=me.headers)).json()

    assert sorted(row["unread"] for row in inbox["data"]) == [1, 2]
    assert inbox["total_unread"] == 3
    assert badge == {"total": 3}


async def test_reading_moves_forward_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "forward")
    older, newer = await say(api_client, mentor, mentee, "one", "two")

    # Reading a chat marks nothing.
    await thread(api_client, mentee, mentor)
    assert (await api_client.get(f"{ROOT}/unread", headers=mentee.headers)).json()["total"] == 2

    assert (await mark(api_client, mentee, mentor, older)).status_code == 204
    assert (await api_client.get(f"{ROOT}/unread", headers=mentee.headers)).json()["total"] == 1
    assert (await mark(api_client, mentee, mentor, newer)).status_code == 204
    moved = (await thread(api_client, mentor, mentee)).json()["their_last_read_at"]
    assert (await mark(api_client, mentee, mentor, older)).status_code == 204

    after = (await thread(api_client, mentor, mentee)).json()
    mine = (await thread(api_client, mentee, mentor)).json()
    assert after["their_last_read_at"] == moved
    assert mine["my_last_read_at"] == moved
    assert after["my_last_read_at"] is None
    assert (await api_client.get(f"{ROOT}/unread", headers=mentee.headers)).json()["total"] == 0


async def test_a_read_mark_must_be_a_message_in_this_chat(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "mark-other")
    other_mentor = await person(db_engine, "mark-other-2", mentor="listed")
    await book(db_engine, other_mentor, mentee, starts_in=5 * DAY)
    (elsewhere,) = await say(api_client, other_mentor, mentee, "elsewhere")

    empty = await mark(api_client, mentee, mentor, elsewhere)
    await say(api_client, mentor, mentee, "here")
    wrong = await mark(api_client, mentee, mentor, elsewhere)

    for response in (empty, wrong):
        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "/up_to"


async def test_a_chat_without_a_connection_is_stored_and_counted_nowhere(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A legacy chat between two people who never booked stays hidden: no row,
    no unread, no thread."""
    mentor, mentee = await pair(db_engine, "hidden", status="declined")
    async with db_engine.begin() as conn:
        chat = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                    "VALUES ('accepted', :m, :e) RETURNING id"
                ),
                {"m": mentor.id, "e": mentee.id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO messages (conversation_id, sender_id, message_type, body, "
                "legacy_bubble_id) VALUES (:c, :m, 'text', 'Old', 'b1')"
            ),
            {"c": chat, "m": mentor.id},
        )

    assert (await api_client.get(f"{ROOT}/unread", headers=mentee.headers)).json() == {"total": 0}
    assert (await api_client.get(ROOT, headers=mentee.headers)).json()["data"] == []
    assert (await thread(api_client, mentee, mentor)).status_code == 404

    # Booking later shows the history they already had.
    await book(db_engine, mentor, mentee, starts_in=9 * DAY)
    assert (await api_client.get(f"{ROOT}/unread", headers=mentee.headers)).json() == {"total": 1}


# --------------------------------------------------------------------------
# A chat's pages
# --------------------------------------------------------------------------


async def test_pages_read_backward_and_forward(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "pages")
    ids = await say(api_client, mentee, mentor, "1", "2", "3", "4", "5")

    assert await bodies(api_client, mentor, mentee) == (["1", "2", "3", "4", "5"], False)
    assert await bodies(api_client, mentor, mentee, limit=2) == (["4", "5"], True)
    assert await bodies(api_client, mentor, mentee, before=ids[3], limit=2) == (["2", "3"], True)
    assert await bodies(api_client, mentor, mentee, before=ids[1], limit=2) == (["1"], False)
    assert await bodies(api_client, mentor, mentee, after=ids[2]) == (["4", "5"], False)
    assert await bodies(api_client, mentor, mentee, after=ids[2], limit=1) == (["4"], True)
    assert await bodies(api_client, mentor, mentee, after=ids[4]) == ([], False)
    assert await bodies(api_client, mentor, mentee, limit=0) == ([], True)
    assert await bodies(api_client, mentor, mentee, before=ids[0], limit=0) == ([], False)


async def test_an_empty_chat_is_a_page_with_no_items(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "empty")

    page = await thread(api_client, mentee, mentor, limit=0)

    assert page.status_code == 200
    body = page.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["lock"] is None
    assert body["my_last_read_at"] is None
    assert body["their_last_read_at"] is None
    assert page.headers["cache-control"] == "private, no-store"


async def test_a_bad_page_request_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "bad-page")
    other = await person(db_engine, "bad-page-2", mentor="listed")
    await book(db_engine, other, mentee, starts_in=5 * DAY)
    (here,) = await say(api_client, mentee, mentor, "here")
    (there,) = await say(api_client, mentee, other, "there")

    for params in (
        {"before": here, "after": here},
        {"before": there},
        {"after": str(uuid4())},
        {"limit": 101},
        {"limit": -1},
    ):
        response = await thread(api_client, mentee, mentor, **params)
        assert response.status_code == 422, params


# --------------------------------------------------------------------------
# The next booking and the session window
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "starts_in", "window", "next_booking"),
    [
        ("confirmed", dt.timedelta(minutes=14), True, True),
        ("confirmed", dt.timedelta(minutes=16), False, True),
        ("confirmed", dt.timedelta(minutes=-14), True, False),
        ("confirmed", dt.timedelta(minutes=-16), False, False),
        ("completed", dt.timedelta(minutes=-10), True, False),
        ("cancelled", dt.timedelta(minutes=5), False, False),
    ],
)
async def test_the_session_window_is_fifteen_minutes_either_side_of_a_start(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    status: str,
    starts_in: dt.timedelta,
    window: bool,
    next_booking: bool,
) -> None:
    mentor, mentee = await pair(
        db_engine,
        f"window-{status}-{starts_in.total_seconds():g}",
        status=status,
        starts_in=starts_in,
    )

    page = (await thread(api_client, mentee, mentor)).json()

    assert page["session_window"] is window
    assert (page["next_booking"] is not None) is next_booking


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


async def test_people_match_a_live_name_ignoring_case(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await person(db_engine, "people-me")
    ada = await person(db_engine, "people-ada", first_name="Ada", last_name="Obi", mentor="listed")
    bea = await person(db_engine, "people-bea", first_name="Bea", last_name="Ade", mentor="listed")
    gone = await person(db_engine, "people-gone", first_name="Adaeze", mentor="listed")
    for n, mentor in enumerate((ada, bea, gone)):
        await book(db_engine, mentor, me, starts_in=(n + 2) * DAY)
    await close_account(db_engine, gone)

    def names(response: httpx.Response) -> list[Any]:
        return [p["first_name"] for p in response.json()["data"]]

    everyone = await api_client.get(f"{ROOT}/people", headers=me.headers)
    ad = await api_client.get(f"{ROOT}/people", params={"q": " ad "}, headers=me.headers)
    full = await api_client.get(f"{ROOT}/people", params={"q": "ADA OBI"}, headers=me.headers)
    wild = await api_client.get(f"{ROOT}/people", params={"q": "%"}, headers=me.headers)
    long = await api_client.get(f"{ROOT}/people", params={"q": "x" * 101}, headers=me.headers)

    assert names(everyone) == ["Ada", "Bea", None]
    assert names(ad) == ["Ada", "Bea"]
    assert names(full) == ["Ada"]
    assert names(wild) == []
    assert everyone.json()["next_cursor"] is None
    assert long.status_code == 422


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


async def test_a_page_and_the_badge_read_an_index(db_engine: AsyncEngine) -> None:
    """The guardrail, at 30,000 messages across 300 chats: a page and the
    unread count read `ix_messages_conversation_sent`, never a scan."""
    mentor, mentee = await pair(db_engine, "plan")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, first_name, primary_role, timezone) "
                "SELECT 'bulk' || n || '@example.test', 'P', 'mentee', 'UTC' "
                "FROM generate_series(1, 300) n"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                "SELECT 'accepted', :m, id FROM users WHERE email LIKE 'bulk%'"
            ),
            {"m": mentor.id},
        )
        await conn.execute(
            text(
                "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) "
                "VALUES ('accepted', :m, :e)"
            ),
            {"m": mentor.id, "e": mentee.id},
        )
        await conn.execute(
            text(
                "INSERT INTO messages (conversation_id, sender_id, message_type, body, sent_at) "
                "SELECT c.id, c.mentor_user_id, 'text', 'x', now() - n * interval '1 minute' "
                "FROM conversations c, generate_series(1, 100) n"
            )
        )
        await conn.execute(text("ANALYZE"))
        chat = (
            await conn.execute(
                text("SELECT id FROM conversations WHERE mentee_user_id = :e"), {"e": mentee.id}
            )
        ).scalar_one()

    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "messages" in statement:
            captured.append((statement, parameters))

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with async_sessionmaker(db_engine)() as session:
            await thread_page(session, chat, before=None, after=None, limit=50)
            await unread_total(session, UUID(str(mentee.id)))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)

    async with db_engine.connect() as conn:
        for statement, parameters in captured:
            rows = (await conn.exec_driver_sql(f"EXPLAIN {statement}", parameters)).all()
            plan = "\n".join(row[0] for row in rows)
            assert "ix_messages_conversation_sent" in plan, plan
            assert "Seq Scan on messages" not in plan, plan
