"""The admin Emails pages, `/api/v1/admin/emails` (PRD 28, decision 321).

**What carries weight**: the gate; a save that edits, resets, keeps an edit or
changes nothing, each with the history it writes; the 409 that names who saved
first; the 422's entries; the 423 for an email that is not set up; and the list's
"Last edited". Whether an email is Live is stubbed here (`_live`); its own rule is
pinned in `test_admin_emails_live`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_admin_credit_reads import an_admin
from tests.integration.test_api_admin_credits import a_user

from app.api.deps import admin_emails
from app.domain.email_registry import EMAILS
from app.domain.notifications import Notification
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

EMAILS_URL = "/api/v1/admin/emails"
GRANTED = f"{EMAILS_URL}/credits_granted"
SAVE = f"{GRANTED}/variants/default"


@pytest.fixture(autouse=True)
def every_grouped_email_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_emails, "_live", lambda _settings: frozenset({Notification.CREDITS_GRANTED})
    )


def body(detail: dict[str, Any], **texts: str) -> dict[str, Any]:
    lines = detail["variants"][0]["lines"]
    return {
        "lines": {
            line: {"text": texts.get(line, lines[line]["text"]), "version": lines[line]["version"]}
            for line in ("subject", "heading", "button")
        }
    }


async def history(engine: AsyncEngine) -> list[tuple[str, str, str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT line, action, before_text, after_text FROM email_line_edits "
                "WHERE email_key = 'credits_granted' ORDER BY created_at, id"
            )
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def overrides(engine: AsyncEngine) -> list[tuple[str, str, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT line, override_text, version FROM email_line_overrides "
                "WHERE email_key = 'credits_granted' ORDER BY line"
            )
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def test_only_an_admin_may_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    assert (await api_client.get(EMAILS_URL)).status_code == 401
    nobody = uuid4()
    await a_user(db_engine, nobody)
    # A non-admin is told nothing exists here, as on every admin route.
    assert (await api_client.get(EMAILS_URL, headers=bearer(api_token(nobody)))).status_code == 404
    response = await api_client.get(EMAILS_URL, headers=await an_admin(db_engine))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


async def test_the_list_is_every_email_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    rows = (await api_client.get(EMAILS_URL, headers=await an_admin(db_engine))).json()
    assert rows["next_cursor"] is None
    assert [row["key"] for row in rows["data"]] == [str(e.key) for e in EMAILS]
    granted = next(row for row in rows["data"] if row["key"] == "credits_granted")
    assert granted["status"] == "live"
    assert (granted["group"], granted["group_name"], granted["recipients"]) == (
        "credits",
        "Credits",
        ["mentee"],
    )
    booked = next(row for row in rows["data"] if row["key"] == "session_booked")
    assert booked["status"] == "not_set_up"


async def test_an_email_reads_with_its_variants_lines_and_values(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    detail = (await api_client.get(GRANTED, headers=headers)).json()
    assert detail["siblings"] == 2
    (variant,) = detail["variants"]
    assert variant["id"] == "default"
    assert variant["lines"]["subject"] == {
        "default_text": "Your session credits are here",
        "text": "Your session credits are here",
        "edited": False,
        "version": 0,
        "default_changed": False,
        "new_default": None,
    }
    assert set(variant["sample"]) == {value["name"] for value in detail["values"]}
    booked = (await api_client.get(f"{EMAILS_URL}/session_booked", headers=headers)).json()
    assert [(v["id"], v["label"]) for v in booked["variants"]] == [
        ("mentor", "To the mentor"),
        ("mentee", "To the mentee"),
    ]
    missing = await api_client.get(f"{EMAILS_URL}/nothing_like_this", headers=headers)
    assert (missing.status_code, missing.json()["type"]) == (404, "/problems/email-not-found")


async def test_a_save_edits_resets_and_records_each_change(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    detail = (await api_client.get(GRANTED, headers=headers)).json()

    edited = await api_client.put(
        SAVE, headers=headers, json=body(detail, subject="Credits, {recipientFirstName}")
    )
    assert edited.status_code == 200, edited.text
    line = edited.json()["variants"][0]["lines"]["subject"]
    assert (line["text"], line["edited"], line["version"]) == (
        "Credits, {recipientFirstName}",
        True,
        1,
    )
    assert await overrides(db_engine) == [("subject", "Credits, {recipientFirstName}", 1)]

    again = await api_client.put(SAVE, headers=headers, json=body(edited.json()))
    assert again.status_code == 200
    assert len(await history(db_engine)) == 1

    reset = await api_client.put(
        SAVE, headers=headers, json=body(again.json(), subject="Your session credits are here")
    )
    assert reset.status_code == 200
    # A reset keeps counting: edit 1, reset 2 (never back to 0).
    assert reset.json()["variants"][0]["lines"]["subject"]["version"] == 2
    assert await overrides(db_engine) == []
    assert [row[:2] for row in await history(db_engine)] == [
        ("subject", "edit"),
        ("subject", "reset"),
    ]

    listed = (await api_client.get(EMAILS_URL, headers=headers)).json()["data"]
    granted = next(row for row in listed if row["key"] == "credits_granted")
    assert granted["last_edited"]["admin_name"] == "Ada"


async def test_a_stale_save_is_a_conflict_naming_who_saved_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    read = (await api_client.get(GRANTED, headers=headers)).json()
    first = await api_client.put(SAVE, headers=headers, json=body(read, heading="First"))
    assert first.status_code == 200

    stale = await api_client.put(SAVE, headers=headers, json=body(read, heading="Second"))
    assert stale.status_code == 409
    problem = stale.json()
    assert problem["type"] == "/problems/email-line-conflict"
    assert problem["updated_by_name"] == "Ada"
    assert problem["updated_at"]
    assert await overrides(db_engine) == [("heading", "First", 1)]


async def test_an_invalid_line_names_what_is_wrong_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    headers = await an_admin(db_engine)
    read = (await api_client.get(GRANTED, headers=headers)).json()
    response = await api_client.put(
        SAVE, headers=headers, json=body(read, subject="Hi {recipientfirstname}", button="")
    )
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"] == "/problems/email-line-invalid"
    assert problem["errors"] == [
        {
            "pointer": "/lines/subject/text",
            "message": "unknown_value",
            "line": "subject",
            "code": "unknown_value",
            "value": "recipientfirstname",
            "suggestion": "recipientFirstName",
        },
        {"pointer": "/lines/button/text", "message": "empty", "line": "button", "code": "empty"},
    ]
    assert await overrides(db_engine) == []
    assert await history(db_engine) == []


async def test_an_email_not_set_up_is_read_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_emails, "_live", lambda _settings: frozenset())
    headers = await an_admin(db_engine)
    read = (await api_client.get(GRANTED, headers=headers)).json()
    assert read["status"] == "not_set_up"
    response = await api_client.put(SAVE, headers=headers, json=body(read, subject="Mine"))
    assert (response.status_code, response.json()["type"]) == (423, "/problems/email-not-set-up")
    wrong = await api_client.put(
        f"{GRANTED}/variants/mentor", headers=headers, json=body(read, subject="Mine")
    )
    assert wrong.status_code == 404


async def test_the_values_list_says_where_each_is_used(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    values = (
        await api_client.get("/api/v1/admin/email-values", headers=await an_admin(db_engine))
    ).json()["data"]
    credit = next(value for value in values if value["name"] == "creditCount")
    assert credit["used_in"] == ["Credits granted", "Credits renewed", "Credits expiring"]
    assert credit["fallback"] == "your"


async def test_a_save_after_a_reset_cannot_reuse_an_old_version(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An editor that read the edit (version 1) saves after another admin reset
    the line: the line's count moved on, so it is a 409 naming who reset it."""
    headers = await an_admin(db_engine)
    read = (await api_client.get(GRANTED, headers=headers)).json()
    edited = (await api_client.put(SAVE, headers=headers, json=body(read, subject="Mine"))).json()
    default = read["variants"][0]["lines"]["subject"]["default_text"]
    reset = await api_client.put(SAVE, headers=headers, json=body(edited, subject=default))
    assert reset.json()["variants"][0]["lines"]["subject"]["version"] == 2

    stale = await api_client.put(SAVE, headers=headers, json=body(edited, subject="Late"))
    assert stale.status_code == 409
    assert stale.json()["updated_by_name"] == "Ada"
    assert stale.json()["updated_at"]

    again = await api_client.put(SAVE, headers=headers, json=body(reset.json(), subject="Next"))
    assert again.status_code == 200
    assert again.json()["variants"][0]["lines"]["subject"]["version"] == 3
    assert await overrides(db_engine) == [("subject", "Next", 3)]
