"""Message contents in a preview: hidden until the admin reveals them (decision 314).

In a preview the previewed user's message reads keep their shape and lose
their contents (`body`, `file`, the inbox's `text` and `file_name`), marked
`hidden_in_preview`, until the admin reveals them for that user with a reason.
A file download is `404` while they are hidden. Every assertion on a hidden
read is on the raw body, so nothing hidden leaves the server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.message_helpers import ROOT, Person, pair, send, thread
from tests.integration.test_api_message_files import BUCKET, download, send_file, uploaded
from tests.integration.test_api_preview_sessions import (
    BASE,
    Admin,
    a_user,
    age,
    an_admin,
    start,
)
from tests.integration.test_api_preview_swap import assert_read_only, blocked, previewing

from app.api.deps import preview as preview_deps
from app.core.config import Settings
from app.domain.preview import REVEAL_REASON_MAX_LENGTH
from app.main import create_app
from conftest import FakeStorage, bearer, build_api_app, client_for, storage_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PRIVATE_TEXT = "Meet me at the secret place"
FILE_NOTE = "My essay, attached"

#: What a hidden message withholds; every other field is unchanged.
HIDDEN_MESSAGE = {"body": None, "file": None, "hidden_in_preview": True}
HIDDEN_LAST = {"text": None, "file_name": None, "hidden_in_preview": True}


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app = build_api_app(
        db_engine,
        None,
        Settings(_env_file=None, preview_as_user_enabled=True, supabase_intake_bucket=BUCKET),
    )
    app.state.intake_storage = storage_for(FakeStorage(bucket=BUCKET))
    async with client_for(app) as c:
        yield c


def reveals(preview: str) -> str:
    return f"{BASE}/{preview}/reveals"


async def reveal(
    client: httpx.AsyncClient, admin: Admin, preview: str, reason: str = "Ticket 42"
) -> httpx.Response:
    return await client.post(reveals(preview), json={"reason": reason}, headers=bearer(admin.token))


async def reveal_events(engine: AsyncEngine, preview: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT admin_user_id, target_user_id, field_type, reason FROM preview_events "
                "WHERE session_id = :s AND kind = 'reveal' ORDER BY created_at, id"
            ),
            {"s": preview},
        )
        return [dict(row) for row in rows.mappings()]


async def revealed_at(engine: AsyncEngine, preview: str) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT revealed_at FROM preview_sessions WHERE id = :p"), {"p": preview}
        )


async def a_chat(client: httpx.AsyncClient, engine: AsyncEngine) -> tuple[Person, Person, str]:
    """A mentor and a mentee who wrote each other, a text and a file with a
    note, and the file's id."""
    mentor, mentee = await pair(engine, f"mask-{uuid4().hex[:6]}")
    assert (await send(client, mentor, mentee, PRIVATE_TEXT)).status_code == 201
    file_id = await uploaded(client, mentee)
    sent = await send_file(client, mentee, mentor, file_id, body=FILE_NOTE)
    assert sent.status_code == 201, sent.text
    return mentor, mentee, file_id


async def previewed(
    client: httpx.AsyncClient, engine: AsyncEngine, who: Person
) -> tuple[Admin, str]:
    admin = await an_admin(engine)
    return admin, (await start(client, admin, who.id))["id"]


def without(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in keys}


# --------------------------------------------------------------------------
# Revealing
# --------------------------------------------------------------------------


async def test_a_reveal_shows_the_users_messages_and_logs_why(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine, "mentor_approval")
    user = await a_user(db_engine)
    preview = (await start(client, admin, user))["id"]

    response = await reveal(client, admin, preview, "  Ticket 42: a missing file  ")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["revealed"], body["can_reveal"]) == (preview, True, True)
    assert body["ended_at"] is None
    assert await reveal_events(db_engine, preview) == [
        {
            "admin_user_id": admin.id,
            "target_user_id": user,
            "field_type": "messages",
            "reason": "Ticket 42: a missing file",
        }
    ]
    read = await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))
    assert read.json()["revealed"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "x" * (REVEAL_REASON_MAX_LENGTH + 1)},
        {"reason": 42},
        {"reason": "ok", "field_type": "messages"},
    ],
    ids=["missing", "empty", "blank", "too-long", "not-text", "extra-field"],
)
async def test_a_reveal_needs_a_reason_of_at_most_500_characters(
    client: httpx.AsyncClient, db_engine: AsyncEngine, payload: dict[str, Any]
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await client.post(reveals(preview), json=payload, headers=bearer(admin.token))

    assert response.status_code == 422, response.text
    assert await revealed_at(db_engine, preview) is None
    assert await reveal_events(db_engine, preview) == []


async def test_a_reason_of_exactly_500_characters_is_taken(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await reveal(client, admin, preview, "x" * REVEAL_REASON_MAX_LENGTH)

    assert response.status_code == 200, response.text


@pytest.mark.isolated_db
async def test_a_second_reveal_changes_and_logs_nothing(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    assert (await reveal(client, admin, preview, "first")).status_code == 200
    first = await revealed_at(db_engine, preview)

    again = await reveal(client, admin, preview, "second")

    assert again.status_code == 200, again.text
    assert again.json()["revealed"] is True
    assert await revealed_at(db_engine, preview) == first
    assert [e["reason"] for e in await reveal_events(db_engine, preview)] == ["first"]


async def test_a_reveal_counts_as_activity(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    await age(db_engine, preview, active=20)

    assert (await reveal(client, admin, preview)).status_code == 200

    async with db_engine.connect() as conn:
        fresh = await conn.scalar(
            text(
                "SELECT last_active_at > now() - interval '1 minute' "
                "FROM preview_sessions WHERE id = :p"
            ),
            {"p": preview},
        )
    assert fresh is True


@pytest.mark.parametrize("how", ["ended", "lapsed"])
async def test_an_ended_or_lapsed_preview_takes_no_reveal(
    client: httpx.AsyncClient, db_engine: AsyncEngine, how: str
) -> None:
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]
    if how == "ended":
        await client.post(
            f"{BASE}/{preview}/end", json={"reason": "exit"}, headers=bearer(admin.token)
        )
    else:
        await age(db_engine, preview, started=40, active=31)

    response = await reveal(client, admin, preview)

    assert response.status_code == 409, response.text
    assert await revealed_at(db_engine, preview) is None
    assert await reveal_events(db_engine, preview) == []
    session = (await client.get(f"{BASE}/{preview}", headers=bearer(admin.token))).json()
    assert session["end_reason"] == ("exit" if how == "ended" else "idle")


async def test_only_the_reveal_set_may_reveal_and_can_reveal_says_so(
    client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowed to `super_admin` (always admitted) alone: a narrower grant may
    still preview, gets `404` on a reveal, and is told `can_reveal: false`."""
    monkeypatch.setattr(preview_deps, "REVEAL", frozenset())
    narrow, wide = await an_admin(db_engine), await an_admin(db_engine, "super_admin")
    narrow_preview = await start(client, narrow, await a_user(db_engine))
    wide_preview = await start(client, wide, await a_user(db_engine))

    refused = await reveal(client, narrow, narrow_preview["id"])
    taken = await reveal(client, wide, wide_preview["id"])

    assert narrow_preview["can_reveal"] is False
    assert refused.status_code == 404
    assert refused.json()["type"] == "about:blank"
    assert await revealed_at(db_engine, narrow_preview["id"]) is None
    assert await reveal_events(db_engine, narrow_preview["id"]) == []
    assert wide_preview["can_reveal"] is True
    assert taken.status_code == 200, taken.text


async def test_a_reveal_without_the_permission_is_404_before_its_body_is_read(
    client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_deps, "REVEAL", frozenset())
    admin = await an_admin(db_engine)
    preview = (await start(client, admin, await a_user(db_engine)))["id"]

    response = await client.post(reveals(preview), json={}, headers=bearer(admin.token))

    assert response.status_code == 404


@pytest.mark.isolated_db
async def test_a_reveal_is_for_the_current_user_only(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee, _ = await a_chat(client, db_engine)
    admin, preview = await previewed(client, db_engine, mentee)
    assert (await reveal(client, admin, preview)).status_code == 200
    switched = await client.patch(
        f"{BASE}/{preview}", json={"user_id": str(mentor.id)}, headers=bearer(admin.token)
    )
    assert switched.status_code == 200, switched.text

    page = (await thread(client, Person(mentor.id, previewing(admin, preview)), mentee)).json()

    assert switched.json()["revealed"] is False
    assert await revealed_at(db_engine, preview) is None
    assert [item["hidden_in_preview"] for item in page["items"]] == [True, True]


async def test_the_reveal_body_is_its_own_component() -> None:
    spec = create_app(Settings(_env_file=None, preview_as_user_enabled=True)).openapi()

    operation = spec["paths"]["/api/v1/admin/preview-sessions/{preview_id}/reveals"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]

    assert body == {"$ref": "#/components/schemas/PreviewReveal"}
    reason = spec["components"]["schemas"]["PreviewReveal"]["properties"]["reason"]
    assert (reason["minLength"], reason["maxLength"]) == (1, REVEAL_REASON_MAX_LENGTH)
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PreviewSessionRead"
    }


# --------------------------------------------------------------------------
# Hidden until revealed
# --------------------------------------------------------------------------


@pytest.mark.isolated_db
async def test_a_preview_hides_message_contents_until_revealed(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee, _ = await a_chat(client, db_engine)
    admin, preview = await previewed(client, db_engine, mentee)
    as_previewed = Person(mentee.id, previewing(admin, preview))
    own_thread = (await thread(client, mentee, mentor)).json()
    own_inbox = (await client.get(ROOT, headers=mentee.headers)).json()

    hidden_thread = await thread(client, as_previewed, mentor)
    hidden_inbox = await client.get(ROOT, headers=as_previewed.headers)

    for response in (hidden_thread, hidden_inbox):
        assert response.status_code == 200, response.text
        assert PRIVATE_TEXT not in response.text
        assert FILE_NOTE not in response.text
        assert "Essay draft" not in response.text
    hidden = hidden_thread.json()
    assert [m["body"] for m in own_thread["items"]] == [PRIVATE_TEXT, FILE_NOTE]
    assert [m | HIDDEN_MESSAGE for m in own_thread["items"]] == hidden["items"]
    assert without(hidden, "items") == without(own_thread, "items")
    inbox = hidden_inbox.json()
    assert inbox["data"][0]["last"] == own_inbox["data"][0]["last"] | HIDDEN_LAST
    assert inbox["data"][0]["last"]["kind"] == "file"
    assert without(inbox["data"][0], "last") == without(own_inbox["data"][0], "last")
    assert inbox["total_unread"] == own_inbox["total_unread"]

    assert (await reveal(client, admin, preview)).status_code == 200
    shown_thread = (await thread(client, as_previewed, mentor)).json()
    shown_inbox = (await client.get(ROOT, headers=as_previewed.headers)).json()

    assert shown_thread == own_thread
    assert shown_inbox == own_inbox


async def test_outside_a_preview_nothing_is_hidden(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee, _ = await a_chat(client, db_engine)
    admin = await an_admin(db_engine)
    await start(client, admin, mentee.id)

    page = (await thread(client, mentee, mentor)).json()
    inbox = (await client.get(ROOT, headers=mentee.headers)).json()

    assert [(m["body"], m["hidden_in_preview"]) for m in page["items"]] == [
        (PRIVATE_TEXT, False),
        (FILE_NOTE, False),
    ]
    assert page["items"][1]["file"]["filename"] == "Essay draft.pdf"
    assert inbox["data"][0]["last"]["file_name"] == "Essay draft.pdf"
    assert inbox["data"][0]["last"]["hidden_in_preview"] is False


@pytest.mark.isolated_db
async def test_a_masked_preview_downloads_no_file_until_revealed(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, mentee, sent_file = await a_chat(client, db_engine)
    unsent_file = await uploaded(client, mentee)
    admin, preview = await previewed(client, db_engine, mentee)
    as_previewed = Person(mentee.id, previewing(admin, preview))

    hidden = [await download(client, as_previewed, f) for f in (sent_file, unsent_file)]
    own = await download(client, mentee, sent_file)
    assert (await reveal(client, admin, preview)).status_code == 200
    shown = [await download(client, as_previewed, f) for f in (sent_file, unsent_file)]

    assert [r.status_code for r in hidden] == [404, 404]
    assert all(r.json()["type"] == "about:blank" for r in hidden)
    assert own.status_code == 200
    assert [r.status_code for r in shown] == [200, 200]
    assert shown[0].content == own.content
    assert await blocked(db_engine, preview) == []


@pytest.mark.isolated_db
async def test_a_send_in_a_preview_is_refused_and_stores_nothing(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee, _ = await a_chat(client, db_engine)
    admin, preview = await previewed(client, db_engine, mentee)
    assert (await reveal(client, admin, preview)).status_code == 200

    response = await send(client, Person(mentee.id, previewing(admin, preview)), mentor, "Hi")

    assert_read_only(response)
    async with db_engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM messages WHERE body = 'Hi'"),
        )
    assert count == 0


@pytest.mark.isolated_db
async def test_reading_chats_in_a_preview_marks_nothing_read(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee, _ = await a_chat(client, db_engine)
    admin, preview = await previewed(client, db_engine, mentee)
    as_previewed = Person(mentee.id, previewing(admin, preview))

    async def read_state() -> list[Any]:
        async with db_engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT user_id, last_read_at FROM conversation_participants ORDER BY user_id")
            )
            return [tuple(row) for row in rows]

    before = await read_state()
    unread_before = (await client.get(f"{ROOT}/unread", headers=mentee.headers)).json()
    for revealed in (False, True):
        if revealed:
            assert (await reveal(client, admin, preview)).status_code == 200
        assert (await thread(client, as_previewed, mentor)).status_code == 200
        assert (await client.get(ROOT, headers=as_previewed.headers)).status_code == 200
        assert (await client.get(f"{ROOT}/unread", headers=as_previewed.headers)).json() == (
            unread_before
        )
    marked = await client.post(
        f"{ROOT}/{mentor.id}/read", json={"up_to": str(uuid4())}, headers=as_previewed.headers
    )

    assert_read_only(marked)
    assert await read_state() == before
    assert unread_before["total"] == 1
