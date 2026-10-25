"""A party who missed a session says why (Session Join PRD, 2026-10-21).

`POST /sessions/{id}/missed-reason` is open to a party whose own attendance is
`no_show` on a settled session, once. Only admins read the reasons back; the
other party never sees one, and `SessionRead.missed_reason_sent` says only
whether the **viewer** has sent theirs.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_account_purge import FakeAdmin, FakeStorage, deleted, purge
from tests.integration.test_api_mentee_attendance_rate import a_pair

from app.domain.missed_reasons import NOTE_MAX_LENGTH
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def a_session(
    engine: AsyncEngine,
    pair: dict[str, Any],
    *,
    status: str = "no_show",
    mentor: str | None = "attended",
    mentee: str | None = "no_show",
) -> UUID:
    """One session in ``status``, with each party's attendance (``None``: no row)."""
    async with engine.begin() as conn:
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, "
                    "status) VALUES (:m, :e, :s, 45, :status) RETURNING id"
                ),
                {
                    "m": pair["mentor"],
                    "e": pair["mentee"],
                    "s": dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
                    "status": status,
                },
            )
        ).scalar_one()
        for role, attendance in (("mentor", mentor), ("mentee", mentee)):
            if attendance is not None:
                await conn.execute(
                    text(
                        "INSERT INTO session_participants "
                        "(session_id, user_id, role, attendance_status) VALUES (:s, :u, :r, :a)"
                    ),
                    {"s": session_id, "u": pair[role], "r": role, "a": attendance},
                )
    return UUID(str(session_id))


async def send(
    client: httpx.AsyncClient, session_id: UUID, headers: dict[str, str], **body: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/sessions/{session_id}/missed-reason",
        json=body or {"reason": "forgot"},
        headers=headers,
    )


async def stored(engine: AsyncEngine, session_id: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT user_id, reason, note FROM session_missed_reasons "
                "WHERE session_id = :s ORDER BY created_at, id"
            ),
            {"s": session_id},
        )
        return [dict(r) for r in rows.mappings()]


async def a_stranger(engine: AsyncEngine, tag: str, *, admin: str | None = None) -> Any:
    auth = uuid4()
    async with engine.begin() as conn:
        user = await add_user(conn, f"{tag}@example.test", auth_id=auth)
        if admin is not None:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                    "VALUES (:u, 'limited_access', "
                    "CASE WHEN :revoked THEN now() ELSE NULL END)"
                ),
                {"u": user, "revoked": admin == "revoked"},
            )
    return user, bearer(api_token(auth))


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def test_a_party_who_missed_sends_their_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-send")
    session_id = await a_session(db_engine, pair)

    response = await send(
        api_client,
        session_id,
        pair["mentee_headers"],
        reason="wrong_time_zone",
        note="  I read it as my time, not hers.  ",
    )

    assert response.status_code == 201, response.text
    assert response.headers["location"] == f"/api/v1/sessions/{session_id}"
    body = response.json()
    assert set(body) == {"reason", "note", "created_at"}
    assert body["reason"] == "wrong_time_zone"
    assert body["note"] == "I read it as my time, not hers."
    assert dt.datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert await stored(db_engine, session_id) == [
        {
            "user_id": pair["mentee"],
            "reason": "wrong_time_zone",
            "note": "I read it as my time, not hers.",
        }
    ]


@pytest.mark.parametrize("note", ["   ", "", "\u0000"])
async def test_a_blank_note_is_stored_as_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, note: str
) -> None:
    pair = await a_pair(db_engine, f"mr-blank-{len(note)}-{ord(note[0]) if note else 0}")
    session_id = await a_session(db_engine, pair)

    response = await send(api_client, session_id, pair["mentee_headers"], reason="other", note=note)

    assert response.status_code == 201, response.text
    assert response.json()["note"] is None
    assert (await stored(db_engine, session_id))[0]["note"] is None


async def test_both_parties_may_send_when_both_missed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-both")
    session_id = await a_session(db_engine, pair, mentor="no_show", mentee="no_show")

    mentor = await send(api_client, session_id, pair["mentor_headers"], reason="emergency")
    mentee = await send(api_client, session_id, pair["mentee_headers"], reason="technical")

    assert (mentor.status_code, mentee.status_code) == (201, 201)
    assert {r["reason"] for r in await stored(db_engine, session_id)} == {"emergency", "technical"}


async def test_a_mentor_who_missed_a_completed_status_session_may_send(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`completed` is settled too: a migrated row can say completed with the
    mentor's own row absent, and their row is what counts."""
    pair = await a_pair(db_engine, "mr-completed")
    session_id = await a_session(
        db_engine, pair, status="completed", mentor="no_show", mentee="attended"
    )

    response = await send(api_client, session_id, pair["mentor_headers"])

    assert response.status_code == 201, response.text


async def test_a_second_send_is_a_409(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-twice")
    session_id = await a_session(db_engine, pair)
    assert (await send(api_client, session_id, pair["mentee_headers"])).status_code == 201

    again = await send(api_client, session_id, pair["mentee_headers"], reason="other")

    assert again.status_code == 409, again.text
    assert again.json()["type"] == "/problems/missed-reason-already-sent"
    assert [r["reason"] for r in await stored(db_engine, session_id)] == ["forgot"]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


async def test_anyone_not_a_party_gets_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-stranger")
    session_id = await a_session(db_engine, pair)
    _, stranger = await a_stranger(db_engine, "mr-stranger-x")
    # An admin is not a party either: this is a party's own statement.
    _, admin = await a_stranger(db_engine, "mr-stranger-admin", admin="live")

    for headers in (stranger, admin):
        response = await send(api_client, session_id, headers)
        assert response.status_code == 404, response.text
    missing = await send(api_client, uuid4(), pair["mentee_headers"])
    assert missing.status_code == 404
    assert await stored(db_engine, session_id) == []


async def test_no_token_is_401(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    pair = await a_pair(db_engine, "mr-anon")
    session_id = await a_session(db_engine, pair)

    assert (await send(api_client, session_id, {})).status_code == 401


@pytest.mark.parametrize(
    "status", ["pending_mentor_approval", "confirmed", "cancelled", "declined", "expired"]
)
async def test_a_session_not_settled_is_a_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    """Before 403: an unsettled session is not missed by anyone yet, even with
    the caller's row already saying `no_show`."""
    pair = await a_pair(db_engine, f"mr-unsettled-{status}")
    session_id = await a_session(db_engine, pair, status=status, mentee="no_show")

    response = await send(api_client, session_id, pair["mentee_headers"])

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "/problems/session-not-missed"
    assert await stored(db_engine, session_id) == []


@pytest.mark.parametrize(
    ("status", "mentor", "mentee"),
    [
        ("completed", "attended", "attended"),
        ("completed", "attended", "pending"),
        ("no_show", "attended", None),
        ("no_show", None, None),
    ],
)
async def test_a_settled_session_nobody_missed_is_a_422(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    status: str,
    mentor: str | None,
    mentee: str | None,
) -> None:
    """Nobody to explain: both attended, or no row says `no_show`. A `422`,
    not the `403`, which is for a session the *other* party missed."""
    pair = await a_pair(db_engine, f"mr-nobody-{status}-{mentor}-{mentee}".lower())
    session_id = await a_session(db_engine, pair, status=status, mentor=mentor, mentee=mentee)

    for headers in (pair["mentor_headers"], pair["mentee_headers"]):
        response = await send(api_client, session_id, headers)
        assert response.status_code == 422, response.text
        assert response.json()["type"] == "/problems/session-not-missed"
    assert await stored(db_engine, session_id) == []


@pytest.mark.parametrize(
    ("status", "mine"),
    [("no_show", "attended"), ("completed", "attended"), ("no_show", "pending"), ("no_show", None)],
)
async def test_a_party_who_did_not_miss_a_settled_session_gets_403(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, status: str, mine: str | None
) -> None:
    pair = await a_pair(db_engine, f"mr-mine-{status}-{mine}".lower())
    session_id = await a_session(db_engine, pair, status=status, mentor=mine, mentee="no_show")

    response = await send(api_client, session_id, pair["mentor_headers"])

    assert response.status_code == 403, response.text
    assert response.json()["type"] == "/problems/missed-reason-not-yours"
    assert await stored(db_engine, session_id) == []


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "late"},
        {},
        {"reason": "forgot", "note": "x" * (NOTE_MAX_LENGTH + 1)},
        {"reason": "forgot", "user_id": str(uuid4())},
    ],
)
async def test_a_bad_body_is_a_request_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, Any]
) -> None:
    pair = await a_pair(db_engine, f"mr-bad-{len(str(body))}")
    session_id = await a_session(db_engine, pair)

    response = await api_client.post(
        f"/api/v1/sessions/{session_id}/missed-reason", json=body, headers=pair["mentee_headers"]
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "about:blank"
    assert await stored(db_engine, session_id) == []


async def test_a_note_at_the_bound_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-bound")
    session_id = await a_session(db_engine, pair)

    response = await send(
        api_client, session_id, pair["mentee_headers"], reason="other", note="é" * NOTE_MAX_LENGTH
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# `missed_reason_sent`, the viewer's own
# --------------------------------------------------------------------------


async def flag(client: httpx.AsyncClient, session_id: UUID, headers: dict[str, str]) -> Any:
    response = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["missed_reason_sent"]


async def listed_flag(
    client: httpx.AsyncClient, user: UUID, session_id: UUID, headers: dict[str, str]
) -> Any:
    response = await client.get(f"/api/v1/users/{user}/sessions", headers=headers)
    assert response.status_code == 200, response.text
    (row,) = [r for r in response.json()["data"] if r["id"] == str(session_id)]
    return row["missed_reason_sent"]


async def test_missed_reason_sent_is_the_viewers_own(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-flag")
    session_id = await a_session(db_engine, pair)
    other = await a_session(db_engine, pair)
    assert await flag(api_client, session_id, pair["mentee_headers"]) is False

    assert (await send(api_client, session_id, pair["mentee_headers"])).status_code == 201

    assert await flag(api_client, session_id, pair["mentee_headers"]) is True
    assert await listed_flag(api_client, pair["mentee"], session_id, pair["mentee_headers"])
    # The other party learns nothing: their own flag, and no reason anywhere.
    assert await flag(api_client, session_id, pair["mentor_headers"]) is False
    assert not await listed_flag(api_client, pair["mentor"], session_id, pair["mentor_headers"])
    detail = await api_client.get(f"/api/v1/sessions/{session_id}", headers=pair["mentor_headers"])
    events = await api_client.get(
        f"/api/v1/sessions/{session_id}/events", headers=pair["mentor_headers"]
    )
    assert "forgot" not in detail.text and "forgot" not in events.text
    # Another session of the same viewer is untouched.
    assert await flag(api_client, other, pair["mentee_headers"]) is False


async def test_an_admin_listing_someone_elses_sessions_sees_their_own_flag(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The list is scoped to the user in the path, the flag to the viewer."""
    pair = await a_pair(db_engine, "mr-admin-list")
    session_id = await a_session(db_engine, pair)
    assert (await send(api_client, session_id, pair["mentee_headers"])).status_code == 201
    _, admin = await a_stranger(db_engine, "mr-admin-list-x", admin="live")

    assert await listed_flag(api_client, pair["mentee"], session_id, admin) is False
    assert await listed_flag(api_client, pair["mentee"], session_id, pair["mentee_headers"])


# --------------------------------------------------------------------------
# The admin read
# --------------------------------------------------------------------------


async def admin_read(
    client: httpx.AsyncClient, session_id: UUID, headers: dict[str, str]
) -> httpx.Response:
    return await client.get(f"/api/v1/admin/sessions/{session_id}/missed-reasons", headers=headers)


async def test_an_admin_reads_every_reason_with_the_role(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-admin")
    session_id = await a_session(db_engine, pair, mentor="no_show", mentee="no_show")
    await send(api_client, session_id, pair["mentee_headers"], reason="forgot", note="Sorry")
    await send(api_client, session_id, pair["mentor_headers"], reason="technical")
    _, admin = await a_stranger(db_engine, "mr-admin-x", admin="live")

    response = await admin_read(api_client, session_id, admin)

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["next_cursor"] is None
    rows = [{k: v for k, v in r.items() if k != "created_at"} for r in page["data"]]
    assert rows == [
        {"user_id": str(pair["mentee"]), "role": "mentee", "reason": "forgot", "note": "Sorry"},
        {"user_id": str(pair["mentor"]), "role": "mentor", "reason": "technical", "note": None},
    ]
    assert all(dt.datetime.fromisoformat(r["created_at"]) for r in page["data"])


async def test_a_session_with_no_reasons_is_an_empty_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pair = await a_pair(db_engine, "mr-admin-empty")
    session_id = await a_session(db_engine, pair)
    _, admin = await a_stranger(db_engine, "mr-admin-empty-x", admin="live")

    response = await admin_read(api_client, session_id, admin)

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert (await admin_read(api_client, uuid4(), admin)).status_code == 404


async def test_only_a_live_admin_reads_reasons(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The parties themselves included: a reason is never read back by a party."""
    pair = await a_pair(db_engine, "mr-admin-gate")
    session_id = await a_session(db_engine, pair, mentor="no_show", mentee="no_show")
    await send(api_client, session_id, pair["mentee_headers"])
    _, revoked = await a_stranger(db_engine, "mr-admin-gate-r", admin="revoked")
    _, live = await a_stranger(db_engine, "mr-admin-gate-l", admin="live")

    assert (await admin_read(api_client, session_id, {})).status_code == 401
    for headers in (pair["mentor_headers"], pair["mentee_headers"], revoked):
        response = await admin_read(api_client, session_id, headers)
        assert response.status_code == 404, response.text
        assert "forgot" not in response.text
    assert (await admin_read(api_client, session_id, live)).status_code == 200


# --------------------------------------------------------------------------
# Account purge
# --------------------------------------------------------------------------


async def test_the_purge_clears_the_note_and_keeps_the_reason(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The note is free text the person wrote (decision 277); the reason is a
    closed fact about a session, kept like the session, unnamed."""
    pair = await a_pair(db_engine, "mr-purge")
    session_id = await a_session(db_engine, pair, mentor="no_show", mentee="no_show")
    await send(api_client, session_id, pair["mentee_headers"], reason="other", note="My words")
    await send(api_client, session_id, pair["mentor_headers"], reason="other", note="Theirs")
    await deleted(db_engine, pair["mentee"], purge_in=-dt.timedelta(days=1))

    counts = await purge(db_engine, FakeAdmin(), FakeStorage())

    assert counts["purged"] == 1
    rows = {r["user_id"]: r for r in await stored(db_engine, session_id)}
    assert rows[pair["mentee"]]["note"] is None
    assert rows[pair["mentee"]]["reason"] == "other"
    assert rows[pair["mentor"]]["note"] == "Theirs"
