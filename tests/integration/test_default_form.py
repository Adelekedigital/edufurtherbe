"""The default offering's form and its one-off adoption (decision 367).

`adopt_default_form` renames an untouched legacy default and gives it the two
questions; everything a mentor changed is left alone. The load writes the same
form on a type that never had a question, so a re-run adds nothing.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from app.domain.intake import DEFAULT_SESSION_TYPE_NAME
from app.domain.transform.sessions import GENERAL_MENTORSHIP
from app.infra.db.default_form import (
    adopt_default_form,
    give_default_form,
    untouched_defaults,
)
from app.infra.db.engine import create_session_factory
from app.infra.db.intake_store import as_asked, questions_by_type

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _adopt(engine: AsyncEngine, type_id: UUID) -> str:
    async with create_session_factory(engine)() as session:
        outcome = await adopt_default_form(session, type_id)
        await session.commit()
    return outcome


async def _due(engine: AsyncEngine) -> list[UUID]:
    async with create_session_factory(engine)() as session:
        return await untouched_defaults(session)


async def _form(engine: AsyncEngine, type_id: UUID) -> list[dict[str, object]]:
    async with create_session_factory(engine)() as session:
        return (await questions_by_type(session, [type_id])).get(type_id, [])


async def _row(engine: AsyncEngine, type_id: UUID) -> tuple[str, str | None, int]:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT t.name, t.description, c.duration_minutes FROM session_types t "
                "JOIN session_type_booking_configs c ON c.session_type_id = t.id WHERE t.id = :t"
            ),
            {"t": type_id},
        )
        name, description, minutes = row.one()
    return name, description, minutes


async def _add_question(engine: AsyncEngine, type_id: UUID, *, deleted: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_type_questions "
                "(session_type_id, question_text, question_type, deleted_at) "
                "VALUES (:t, 'Mine', 'free_text', CASE WHEN :d THEN now() END)"
            ),
            {"t": type_id, "d": deleted},
        )


async def _give_help_area(engine: AsyncEngine, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings WHERE slug = 'test-preparation'"
            ),
            {"u": mentor},
        )


async def test_an_untouched_default_is_renamed_and_given_both_questions(
    db_engine: AsyncEngine,
) -> None:
    mentor = await make_public_mentor(db_engine, "adopt")
    await _give_help_area(db_engine, mentor)
    type_id = await add_session_type(db_engine, mentor, name=GENERAL_MENTORSHIP, duration=30)

    assert await _due(db_engine) == [type_id]
    assert await _adopt(db_engine, type_id) == "adopted"

    assert await _row(db_engine, type_id) == (DEFAULT_SESSION_TYPE_NAME, None, 30)
    form = await _form(db_engine, type_id)
    assert [
        (q["question_text"], q["question_type"], q["is_required"], q["allows_multiple"])
        for q in form
    ] == [
        ("Which area do you need help with most?", "multi_choice", True, False),
        ("What would you like to talk about?", "free_text", True, False),
    ]
    assert [q["options_source"] for q in form] == ["help_areas", None]
    # The topic's options are the mentor's live help areas, read at every load.
    assert [o["text"] for o in form[0]["options"]] == ["Test preparation"]
    async with db_engine.connect() as conn:
        authors = await conn.execute(
            text("SELECT created_by FROM session_type_questions WHERE session_type_id = :t"),
            {"t": type_id},
        )
        assert set(authors.scalars()) == {None}
    assert await _due(db_engine) == []


async def test_a_mentor_with_no_help_area_is_not_blocked_by_the_topic(
    db_engine: AsyncEngine,
) -> None:
    mentor = await make_public_mentor(db_engine, "no-areas")
    type_id = await add_session_type(db_engine, mentor, name=GENERAL_MENTORSHIP)
    await _adopt(db_engine, type_id)

    topic, message = (as_asked(q) for q in await _form(db_engine, type_id))
    assert topic["is_required"] is False
    assert message["is_required"] is True


@pytest.mark.parametrize(
    ("name", "description", "question"),
    [
        (GENERAL_MENTORSHIP, "My own words", None),
        ("My sessions", None, None),
        (GENERAL_MENTORSHIP, None, "live"),
        # Removing the questions is a choice: they are not put back.
        (GENERAL_MENTORSHIP, None, "deleted"),
    ],
)
async def test_a_default_the_mentor_changed_is_left_alone(
    db_engine: AsyncEngine, name: str, description: str | None, question: str | None
) -> None:
    mentor = await make_public_mentor(db_engine, "changed")
    type_id = await add_session_type(db_engine, mentor, name=name, description=description)
    if question is not None:
        await _add_question(db_engine, type_id, deleted=question == "deleted")

    assert await _due(db_engine) == []
    assert await _adopt(db_engine, type_id) == "changed"
    assert (await _row(db_engine, type_id))[:2] == (name, description)


async def test_a_blank_description_is_empty(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "blank")
    type_id = await add_session_type(db_engine, mentor, name=GENERAL_MENTORSHIP, description="  ")

    assert await _due(db_engine) == [type_id]


async def test_a_deleted_default_is_not_adopted(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "deleted")
    await add_session_type(db_engine, mentor, name=GENERAL_MENTORSHIP, deleted=True)

    assert await _due(db_engine) == []


async def test_a_taken_name_is_reported_not_overwritten(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "taken")
    type_id = await add_session_type(db_engine, mentor, name=GENERAL_MENTORSHIP)
    await add_session_type(db_engine, mentor, name=DEFAULT_SESSION_TYPE_NAME)

    assert await _adopt(db_engine, type_id) == "name_taken"
    assert await _row(db_engine, type_id) == (GENERAL_MENTORSHIP, None, 45)
    assert await _form(db_engine, type_id) == []


async def test_the_form_is_written_once(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "once")
    type_id = await add_session_type(db_engine, mentor, name=DEFAULT_SESSION_TYPE_NAME)

    async with db_engine.begin() as conn:
        assert await give_default_form(conn, type_id) is True
        assert await give_default_form(conn, type_id) is False

    assert len(await _form(db_engine, type_id)) == 2
