"""Loading the legacy chats (decision 302): one chat per pair, roles from the
database, read marks forward only, Bubble's timestamps intact, and a re-run
that adds nothing. A connected pair then reads its history through the API;
anyone else reads nothing (M0 decides on read)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.message_helpers import Person, book, person, scalar, thread

from app.domain.transform.messages import plan_messages
from app.infra.etl.messages import MessageLoader
from app.infra.etl.reconcile import reconcile_messages

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

NEW_YORK = ZoneInfo("America/New_York")


def starter(anchor: str, by: str, to: str, **extra: Any) -> dict[str, Any]:
    return {
        "unique id": anchor,
        "sendBy": by,
        "receiveBy": to,
        "messageRequestAccept": "yes",
        "Creation Date": "Jan 5, 2025 9:00 am",
        "Modified Date": "Jan 6, 2025 9:00 am",
    } | extra


def message(anchor: str, chat: str, by: str, sent: str, **extra: Any) -> dict[str, Any]:
    return {
        "unique id": anchor,
        "messageStarter": chat,
        "sendBy": by,
        "messageContent": f"text of {anchor}",
        "sentAt": sent,
        "Creation Date": sent,
        "Modified Date": "Jan 9, 2025 9:00 am",
        "seenRead": "no",
        "softDelete": "no",
    } | extra


async def legacy(engine: AsyncEngine, who: Person) -> str:
    """Give a test person a Bubble anchor."""
    anchor = f"17000x{uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET legacy_bubble_id = :a WHERE id = :u"), {"a": anchor, "u": who.id}
        )
    return anchor


async def users(engine: AsyncEngine) -> dict[str, UUID]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT legacy_bubble_id, id FROM users WHERE legacy_bubble_id IS NOT NULL")
        )
        return {row.legacy_bubble_id: row.id for row in rows}


async def load(engine: AsyncEngine, starters: list[Any], threads: list[Any]) -> Any:
    plan = plan_messages(starters, threads, timezone=NEW_YORK)
    async with engine.begin() as conn:
        await MessageLoader(conn).load(users=await users(engine), plan=plan)
        return await reconcile_messages(conn, plan)


async def snapshot(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            await conn.execute(
                text(
                    "SELECT c.id, c.mentor_user_id, c.mentee_user_id, c.legacy_bubble_id, "
                    "c.last_message_at, m.legacy_bubble_id AS message, m.created_at, "
                    "m.updated_at, m.sent_at, p.user_id, p.last_read_at "
                    "FROM conversations c JOIN messages m ON m.conversation_id = c.id "
                    "JOIN conversation_participants p ON p.conversation_id = c.id "
                    "ORDER BY m.legacy_bubble_id, p.user_id"
                )
            )
        )


async def test_two_bubble_chats_of_one_pair_load_as_one_and_a_rerun_adds_nothing(
    db_engine: AsyncEngine,
) -> None:
    mentor = await person(db_engine, "etl-mentor", slug="etl-mentor", mentor="listed")
    mentee = await person(db_engine, "etl-mentee")
    await book(db_engine, mentor, mentee)
    ada, mo = await legacy(db_engine, mentor), await legacy(db_engine, mentee)
    starters = [
        starter("s-late", ada, mo, **{"Creation Date": "Feb 1, 2025 9:00 am"}),
        starter("s-early", mo, ada, messageRequestAccept="no"),
    ]
    threads = [
        message("m1", "s-early", mo, "Jan 5, 2025 9:01 am"),
        message("m2", "s-late", ada, "Feb 1, 2025 9:05 am", seenRead="yes"),
    ]

    first = await load(db_engine, starters, threads)
    before = await snapshot(db_engine)
    again = await load(db_engine, starters, threads)

    assert first.ok, first.report()
    assert again.ok, again.report()
    assert await snapshot(db_engine) == before
    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 1
    assert await scalar(db_engine, "SELECT count(*) FROM conversation_participants") == 2
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 2
    row = before[0]
    # The booking names the mentor, not who wrote first.
    assert (row.mentor_user_id, row.mentee_user_id) == (mentor.id, mentee.id)
    assert row.legacy_bubble_id == "s-early"
    assert row.last_message_at == dt.datetime(2025, 2, 1, 9, 5, tzinfo=NEW_YORK)
    # Bubble's timestamps, not the import clock.
    stamps = {r.message: (r.created_at, r.updated_at) for r in before}
    assert stamps["m1"][1] == dt.datetime(2025, 1, 9, 9, 0, tzinfo=NEW_YORK)
    # Ada's message was seen: Mo's read mark is at it; Ada's own is unset.
    reads = {r.user_id: r.last_read_at for r in before}
    assert reads == {mentee.id: dt.datetime(2025, 2, 1, 9, 5, tzinfo=NEW_YORK), mentor.id: None}
    assert await scalar(db_engine, "SELECT status FROM conversations") == "accepted", (
        "any accepted starter accepts the chat"
    )


async def test_a_chat_the_pair_already_began_absorbs_the_history(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await person(db_engine, "etl-live-mentor", slug="etl-live-mentor", mentor="listed")
    mentee = await person(db_engine, "etl-live-mentee")
    await book(db_engine, mentor, mentee)
    sent = await api_client.post(
        f"/api/v1/me/messages/{mentor.id}",
        json={"client_id": str(uuid4()), "body": "Hi from the new app"},
        headers=mentee.headers,
    )
    assert sent.status_code == 201
    read = await api_client.post(
        f"/api/v1/me/messages/{mentee.id}/read",
        json={"up_to": sent.json()["id"]},
        headers=mentor.headers,
    )
    assert read.status_code == 204
    ada, mo = await legacy(db_engine, mentor), await legacy(db_engine, mentee)

    result = await load(
        db_engine,
        [starter("s1", mo, ada)],
        [message("old", "s1", mo, "Jan 5, 2025 9:01 am", seenRead="yes")],
    )

    assert result.ok, result.report()
    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 1
    # The read mark never moves back to the older imported message.
    assert await scalar(
        db_engine,
        "SELECT last_read_at FROM conversation_participants WHERE user_id = :u",
        u=mentor.id,
    ) == dt.datetime.fromisoformat(sent.json()["sent_at"])
    items = (await thread(api_client, mentor, mentee)).json()["items"]
    assert [item["body"] for item in items] == ["text of old", "Hi from the new app"]
    assert items[0]["client_id"] is None


async def test_history_shows_edits_and_deletes_to_the_pair_and_nothing_to_others(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await person(db_engine, "etl-h-mentor", slug="etl-h-mentor", mentor="listed")
    mentee = await person(db_engine, "etl-h-mentee")
    await book(db_engine, mentor, mentee)
    stranger = await person(db_engine, "etl-h-stranger", mentor="listed")
    loner = await person(db_engine, "etl-h-loner")
    ada, mo = await legacy(db_engine, mentor), await legacy(db_engine, mentee)
    zed, kim = await legacy(db_engine, stranger), await legacy(db_engine, loner)

    result = await load(
        db_engine,
        [starter("pair", mo, ada), starter("unbooked", kim, zed)],
        [
            message(
                "edited", "pair", mo, "Jan 5, 2025 9:01 am", dateLastEdited="Jan 5, 2025 9:03 am"
            ),
            message("gone", "pair", ada, "Jan 5, 2025 9:02 am", softDelete="yes"),
            message("hidden", "unbooked", kim, "Jan 5, 2025 9:04 am"),
        ],
    )

    assert result.ok, result.report()
    items = (await thread(api_client, mentee, mentor)).json()["items"]
    assert [(i["body"], i["deleted"], i["edited_at"] is not None) for i in items] == [
        ("text of edited", False, True),
        (None, True, False),
    ]
    # Imported for an unbooked pair (roles from the one mentor profile), and
    # unreachable: M0 is checked on read.
    assert (
        await scalar(
            db_engine,
            "SELECT mentor_user_id FROM conversations WHERE legacy_bubble_id = 'unbooked'",
        )
        == stranger.id
    )
    assert (await thread(api_client, loner, stranger)).status_code == 404


async def test_the_booking_names_the_mentor_between_two_mentors(db_engine: AsyncEngine) -> None:
    mentor = await person(db_engine, "etl-two-a", mentor="listed")
    mentored = await person(db_engine, "etl-two-b", mentor="listed")
    await book(db_engine, mentor, mentored)
    a, b = await legacy(db_engine, mentor), await legacy(db_engine, mentored)

    result = await load(db_engine, [starter("s1", a, b)], [])

    assert result.ok, result.report()
    roles = await scalar(
        db_engine, "SELECT array[mentor_user_id, mentee_user_id] FROM conversations"
    )
    assert roles == [mentor.id, mentored.id]


async def test_a_newer_request_never_agreed_does_not_name_the_mentor(
    db_engine: AsyncEngine,
) -> None:
    """M0's own statuses: a declined request with the roles reversed, newer
    than the confirmed booking, is not a connection and decides nothing."""
    mentor = await person(db_engine, "etl-m0-a", mentor="listed")
    mentee = await person(db_engine, "etl-m0-b", mentor="listed")
    await book(db_engine, mentor, mentee, booked_ago=dt.timedelta(days=30))
    await book(
        db_engine,
        mentee,
        mentor,
        status="declined",
        booked_ago=dt.timedelta(days=1),
        starts_in=dt.timedelta(days=9),
    )
    a, b = await legacy(db_engine, mentor), await legacy(db_engine, mentee)

    result = await load(db_engine, [starter("s1", b, a)], [])

    assert result.ok, result.report()
    roles = await scalar(
        db_engine, "SELECT array[mentor_user_id, mentee_user_id] FROM conversations"
    )
    assert roles == [mentor.id, mentee.id]


async def test_imported_messages_are_never_emailed_and_a_new_earliest_starter_reconciles(
    db_engine: AsyncEngine,
) -> None:
    mentor = await person(db_engine, "etl-mail-a", mentor="listed")
    mentee = await person(db_engine, "etl-mail-b")
    ada, mo = await legacy(db_engine, mentor), await legacy(db_engine, mentee)
    late = starter("late", mo, ada, **{"Creation Date": "Feb 1, 2025 9:00 am"})
    threads = [message("m1", "late", mo, "Feb 1, 2025 9:01 am")]
    assert (await load(db_engine, [late], threads)).ok

    # A later export holds an earlier starter of the same pair: the stored
    # anchor stays, and the run still reconciles.
    again = await load(db_engine, [late, starter("early", ada, mo)], threads)

    assert again.ok, again.report()
    assert await scalar(db_engine, "SELECT legacy_bubble_id FROM conversations") == "late"
    assert (
        await scalar(
            db_engine, "SELECT count(*) FROM messages WHERE emailed_at IS DISTINCT FROM sent_at"
        )
        == 0
    )


async def test_a_former_mentor_without_a_booking_is_still_the_mentor(
    db_engine: AsyncEngine,
) -> None:
    """With no booking, a mentor profile names the mentor, soft-deleted or not
    (`predicates.has_mentor_profile`): the chat is history."""
    former = await person(db_engine, "etl-former-a", mentor="listed")
    mentee = await person(db_engine, "etl-former-b")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": former.id},
        )
    a, b = await legacy(db_engine, former), await legacy(db_engine, mentee)

    result = await load(db_engine, [starter("s1", b, a)], [])

    assert result.ok, result.report()
    roles = await scalar(
        db_engine, "SELECT array[mentor_user_id, mentee_user_id] FROM conversations"
    )
    assert roles == [former.id, mentee.id]


async def test_a_chat_whose_mentor_nothing_names_stops_the_load(db_engine: AsyncEngine) -> None:
    one = await person(db_engine, "etl-amb-a")
    two = await person(db_engine, "etl-amb-b")
    a, b = await legacy(db_engine, one), await legacy(db_engine, two)

    with pytest.raises(LookupError, match="chat s1"):
        await load(db_engine, [starter("s1", a, b)], [])

    assert await scalar(db_engine, "SELECT count(*) FROM conversations") == 0


async def test_an_unknown_person_stops_the_load(db_engine: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="unknown user"):
        await load(db_engine, [starter("s1", "17000xnobody", "17000xnobody2")], [])


async def test_reconciliation_names_a_row_that_did_not_land(db_engine: AsyncEngine) -> None:
    mentor = await person(db_engine, "etl-rec-mentor", mentor="listed")
    mentee = await person(db_engine, "etl-rec-mentee")
    ada, mo = await legacy(db_engine, mentor), await legacy(db_engine, mentee)
    plan = plan_messages(
        [starter("s1", mo, ada)],
        [message("m1", "s1", mo, "Jan 5, 2025 9:01 am")],
        timezone=NEW_YORK,
    )
    async with db_engine.begin() as conn:
        await MessageLoader(conn).load(users=await users(db_engine), plan=plan)
        await conn.execute(text("DELETE FROM messages WHERE legacy_bubble_id = 'm1'"))
        result = await reconcile_messages(conn, plan)

    assert not result.ok
    assert "MISSING m1" in result.report()
