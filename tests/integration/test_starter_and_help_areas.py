"""The starter offering and help-area questions (Session Types PRD, 2026-10-21).

**The starter** is the offering onboarding creates ("Created for you"): set by
`POST /me/session-types {is_starter: true}`, read on both lists, never changed
after, one live per mentor.

**A help-area question** is a `multi_choice` with `options_source: help_areas`:
it stores no options, and every read fills them from the mentor's live help
areas, each `{id: service offering id, text}`. Booking checks an answer against
those, and the answer keeps the name it chose.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import token_for
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, first_slot, key

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

OWN = "/api/v1/me/session-types"


async def mentor_headers(engine: AsyncEngine, mentor: UUID) -> dict[str, str]:
    return bearer(await token_for(engine, mentor, auth_id=uuid4()))


async def offerings(engine: AsyncEngine) -> list[dict[str, Any]]:
    """Every live service offering, in the platform's order."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, display_name FROM service_offerings WHERE is_active ORDER BY sort_order"
            )
        )
        return [{"id": str(r.id), "text": r.display_name} for r in rows]


async def set_help_areas(engine: AsyncEngine, mentor: UUID, ids: list[str]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM mentor_service_offerings WHERE mentor_user_id = :m"), {"m": mentor}
        )
        for offering in ids:
            await conn.execute(
                text(
                    "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                    "VALUES (:m, :o)"
                ),
                {"m": mentor, "o": offering},
            )


def help_area_question(**overrides: Any) -> dict[str, Any]:
    return {
        "question_text": "What would you like help with?",
        "question_type": "multi_choice",
        "options_source": "help_areas",
    } | overrides


def pointers(response: httpx.Response) -> list[str]:
    return [e["pointer"] for e in response.json().get("errors", [])]


# --------------------------------------------------------------------------
# The starter
# --------------------------------------------------------------------------


async def test_the_starter_is_set_on_create_and_read_on_both_lists(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, plain = await a_bookable_offering(db_engine, "starter-read")
    headers = await mentor_headers(db_engine, mentor)

    created = await api_client.post(
        OWN,
        json={"name": "Intro chat", "duration_minutes": 30, "is_starter": True},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    own = {
        t["id"]: t["is_starter"]
        for t in (await api_client.get(OWN, headers=headers)).json()["data"]
    }
    public = await api_client.get(f"/api/v1/users/{mentor}/session-types")
    assert own == {created.json()["id"]: True, str(plain): False}
    assert {t["id"]: t["is_starter"] for t in public.json()["data"]} == own


async def test_a_mentor_holds_one_starter_until_it_is_deleted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _ = await a_bookable_offering(db_engine, "starter-one")
    headers = await mentor_headers(db_engine, mentor)
    first = await api_client.post(
        OWN, json={"name": "Intro chat", "is_starter": True}, headers=headers
    )
    assert first.status_code == 201, first.text

    second = await api_client.post(
        OWN, json={"name": "Another", "is_starter": True}, headers=headers
    )
    assert second.status_code == 409, second.text
    assert "starter" in second.json()["detail"]

    deleted = await api_client.delete(f"{OWN}/{first.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    again = await api_client.post(
        OWN, json={"name": "Another", "is_starter": True}, headers=headers
    )
    assert again.status_code == 201, again.text


async def test_the_starter_cannot_change_after_create(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "starter-patch")
    headers = await mentor_headers(db_engine, mentor)

    for value in (True, False, None):
        patched = await api_client.patch(
            f"{OWN}/{session_type}", json={"is_starter": value}, headers=headers
        )
        assert patched.status_code == 422, patched.text
        assert pointers(patched) == ["/is_starter"]
    # The rest of the PATCH still works.
    renamed = await api_client.patch(
        f"{OWN}/{session_type}", json={"name": "Renamed"}, headers=headers
    )
    assert renamed.status_code == 200, renamed.text


# --------------------------------------------------------------------------
# Help-area questions: writing and reading
# --------------------------------------------------------------------------


async def test_a_help_area_question_reads_the_mentors_live_help_areas(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ha-read")
    headers = await mentor_headers(db_engine, mentor)
    every = await offerings(db_engine)
    await set_help_areas(db_engine, mentor, [every[2]["id"], every[0]["id"]])

    created = await api_client.post(
        f"{OWN}/{session_type}/questions",
        json=help_area_question(allows_multiple=True),
        headers=headers,
    )
    assert created.status_code == 201, created.text

    (question,) = (await api_client.get(f"{OWN}/{session_type}/questions", headers=headers)).json()[
        "data"
    ]
    # The platform's order, not the order they were added in.
    assert question["options"] == [every[0], every[2]]
    assert question["options_source"] == "help_areas"
    assert question["allows_multiple"] is True
    (public,) = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    assert public["questions"][0]["options"] == [every[0], every[2]]

    # A help area dropped from the profile, or retired, leaves the options.
    await set_help_areas(db_engine, mentor, [every[2]["id"]])
    (question,) = (await api_client.get(f"{OWN}/{session_type}/questions", headers=headers)).json()[
        "data"
    ]
    assert question["options"] == [every[2]]


async def test_a_retired_offering_is_not_an_option(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ha-retired")
    headers = await mentor_headers(db_engine, mentor)
    every = await offerings(db_engine)
    await set_help_areas(db_engine, mentor, [every[0]["id"], every[1]["id"]])
    await api_client.post(
        f"{OWN}/{session_type}/questions", json=help_area_question(), headers=headers
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE id = :o"),
            {"o": every[1]["id"]},
        )
    try:
        (question,) = (
            await api_client.get(f"{OWN}/{session_type}/questions", headers=headers)
        ).json()["data"]
        assert question["options"] == [every[0]]
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE service_offerings SET is_active = true WHERE id = :o"),
                {"o": every[1]["id"]},
            )


async def test_a_mentor_written_question_reads_no_options_source(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ha-own")
    headers = await mentor_headers(db_engine, mentor)
    await api_client.post(
        f"{OWN}/{session_type}/questions",
        json={
            "question_text": "Which test?",
            "question_type": "multi_choice",
            "options": [{"text": "IELTS"}, {"text": "TOEFL"}],
        },
        headers=headers,
    )
    (question,) = (await api_client.get(f"{OWN}/{session_type}/questions", headers=headers)).json()[
        "data"
    ]
    assert question["options_source"] is None
    assert [o["text"] for o in question["options"]] == ["IELTS", "TOEFL"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": [{"text": "A"}, {"text": "B"}]},
        {"question_type": "free_text"},
        {"options_source": "my_offerings"},
    ],
)
async def test_a_help_area_question_takes_no_options_and_is_a_choice(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, overrides: dict[str, Any]
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, f"ha-bad-{uuid4().hex[:6]}")
    headers = await mentor_headers(db_engine, mentor)

    refused = await api_client.post(
        f"{OWN}/{session_type}/questions", json=help_area_question(**overrides), headers=headers
    )
    assert refused.status_code == 422, refused.text


async def test_a_help_area_question_is_created_with_its_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Inline on `POST /me/session-types`, as onboarding sends it."""
    mentor, _ = await a_bookable_offering(db_engine, "ha-inline")
    headers = await mentor_headers(db_engine, mentor)
    every = await offerings(db_engine)
    await set_help_areas(db_engine, mentor, [every[0]["id"]])

    created = await api_client.post(
        OWN,
        json={"name": "Intro chat", "is_starter": True, "questions": [help_area_question()]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    (question,) = (
        await api_client.get(f"{OWN}/{created.json()['id']}/questions", headers=headers)
    ).json()["data"]
    assert question["options_source"] == "help_areas"
    assert question["options"] == [every[0]]


async def test_the_source_and_its_options_are_not_edited(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Decision 200's rule: no change across the choice line, so an answer never
    changes meaning. Delete the question and add another instead."""
    mentor, session_type = await a_bookable_offering(db_engine, "ha-patch")
    headers = await mentor_headers(db_engine, mentor)
    every = await offerings(db_engine)
    await set_help_areas(db_engine, mentor, [every[0]["id"], every[1]["id"]])
    help_area = (
        await api_client.post(
            f"{OWN}/{session_type}/questions", json=help_area_question(), headers=headers
        )
    ).json()["id"]
    own = (
        await api_client.post(
            f"{OWN}/{session_type}/questions",
            json={
                "question_text": "Which test?",
                "question_type": "multi_choice",
                "options": [{"text": "IELTS"}, {"text": "TOEFL"}],
            },
            headers=headers,
        )
    ).json()["id"]

    for question, change in (
        (help_area, {"options_source": None}),
        (own, {"options_source": "help_areas"}),
    ):
        refused = await api_client.patch(
            f"{OWN}/{session_type}/questions/{question}", json=change, headers=headers
        )
        assert refused.status_code == 422, refused.text
        assert pointers(refused) == ["/options_source"]

    options = await api_client.patch(
        f"{OWN}/{session_type}/questions/{help_area}",
        json={"options": [{"text": "A"}, {"text": "B"}]},
        headers=headers,
    )
    assert options.status_code == 422, options.text
    # Wording and single-or-several still change.
    edited = await api_client.patch(
        f"{OWN}/{session_type}/questions/{help_area}",
        json={"question_text": "Pick your areas", "allows_multiple": True},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text


# --------------------------------------------------------------------------
# Help-area questions: booking and the answers
# --------------------------------------------------------------------------


async def help_area_offering(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str, *, allows_multiple: bool
) -> dict[str, Any]:
    mentor, session_type = await a_bookable_offering(engine, tag)
    headers = await mentor_headers(engine, mentor)
    every = await offerings(engine)
    await set_help_areas(engine, mentor, [every[0]["id"], every[1]["id"]])
    question = (
        await client.post(
            f"{OWN}/{session_type}/questions",
            json=help_area_question(allows_multiple=allows_multiple),
            headers=headers,
        )
    ).json()["id"]
    return {
        "mentor": mentor,
        "session_type": session_type,
        "question": question,
        "headers": headers,
        "every": every,
    }


async def book(
    client: httpx.AsyncClient, engine: AsyncEngine, form: dict[str, Any], tag: str, ids: list[str]
) -> httpx.Response:
    _, headers = await a_mentee(engine, tag)
    payload = body(
        form["session_type"], await first_slot(client, form["mentor"], form["session_type"])
    )
    payload["answers"] = [{"question_id": form["question"], "option_ids": ids}]
    return await client.post("/api/v1/sessions", json=payload, headers=headers | key())


async def test_an_answer_keeps_the_help_area_it_chose(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    form = await help_area_offering(api_client, db_engine, "ha-book", allows_multiple=True)
    every = form["every"]

    booked = await book(
        api_client, db_engine, form, "ha-book-mentee", [every[1]["id"], every[0]["id"]]
    )
    assert booked.status_code == 201, booked.text
    # The mentor drops both help areas, and one is renamed.
    await set_help_areas(db_engine, form["mentor"], [every[2]["id"]])
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE service_offerings SET display_name = display_name || ' (old)' WHERE id = :o"
            ),
            {"o": every[0]["id"]},
        )
    try:
        answers = await api_client.get(
            f"/api/v1/sessions/{booked.json()['id']}/answers", headers=form["headers"]
        )
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE service_offerings SET display_name = :n WHERE id = :o"),
                {"n": every[0]["text"], "o": every[0]["id"]},
            )
    assert answers.status_code == 200, answers.text
    (answer,) = answers.json()["data"]
    assert answer["question_type"] == "multi_choice"
    # In the platform's order, with the names as chosen.
    assert answer["options"] == [every[0], every[1]]


@pytest.mark.parametrize("which", ["not_held", "two_for_single", "an_option_row"])
async def test_a_help_area_answer_is_checked_against_the_mentors_help_areas(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, which: str
) -> None:
    tag = f"ha-bad-{which}"
    form = await help_area_offering(api_client, db_engine, tag, allows_multiple=False)
    every = form["every"]
    ids = {
        "not_held": [every[2]["id"]],
        "two_for_single": [every[0]["id"], every[1]["id"]],
        "an_option_row": [str(uuid4())],
    }[which]

    refused = await book(api_client, db_engine, form, f"{tag}-mentee", ids)

    assert refused.status_code == 422, refused.text
    assert pointers(refused) == ["/answers/0/option_ids"]


async def test_a_single_help_area_answer_is_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    form = await help_area_offering(api_client, db_engine, "ha-single", allows_multiple=False)

    booked = await book(api_client, db_engine, form, "ha-single-mentee", [form["every"][1]["id"]])

    assert booked.status_code == 201, booked.text
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT a.selected_offering_id, a.selected_option_id, a.option_text "
                    "FROM intake_answers a JOIN intake_submissions s ON s.id = a.submission_id "
                    "WHERE s.session_id = :s"
                ),
                {"s": booked.json()["id"]},
            )
        ).one()
    assert (str(row.selected_offering_id), row.selected_option_id, row.option_text) == (
        form["every"][1]["id"],
        None,
        form["every"][1]["text"],
    )
