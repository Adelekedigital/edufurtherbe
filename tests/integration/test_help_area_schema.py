"""The schema half of help-area questions (Session Types PRD, 2026-10-21).

An answer to a help-area question names a service offering rather than an
option row, so `exactly_one_answer_form` counts a fourth form and the two
uniqueness indexes learn about it. `alembic check` sees none of this.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_intake_schema import add_answer, add_option, add_question, form

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

__all__ = ["form"]


async def _offering(engine: AsyncEngine, position: int = 0) -> UUID:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM service_offerings ORDER BY sort_order OFFSET :p LIMIT 1"),
                {"p": position},
            )
        ).scalar_one()


async def _help_area_question(engine: AsyncEngine, session_type: UUID) -> UUID:
    question = await add_question(engine, session_type, question_type="multi_choice")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_questions SET options_source = 'help_areas' WHERE id = :q"),
            {"q": question},
        )
    return question


async def test_a_chosen_help_area_is_an_answer_on_its_own(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The positive case: an offering alone, several on one question as rows."""
    session_type, _, submission = form
    question = await _help_area_question(db_engine, session_type)

    await add_answer(
        db_engine, submission, question, selected_offering_id=await _offering(db_engine, 0)
    )
    await add_answer(
        db_engine, submission, question, selected_offering_id=await _offering(db_engine, 1)
    )


async def test_a_help_area_beside_another_form_is_refused(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    session_type, _, submission = form
    question = await add_question(db_engine, session_type, question_type="multi_choice")
    option = await add_option(db_engine, question)

    with pytest.raises(IntegrityError, match="exactly_one_answer_form"):
        await add_answer(
            db_engine,
            submission,
            question,
            selected_option_id=option,
            selected_offering_id=await _offering(db_engine),
        )


async def test_a_help_area_is_chosen_once_per_answer(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    session_type, _, submission = form
    question = await _help_area_question(db_engine, session_type)
    offering = await _offering(db_engine)
    await add_answer(db_engine, submission, question, selected_offering_id=offering)

    with pytest.raises(IntegrityError, match="ux_intake_answers_one_per_offering"):
        await add_answer(db_engine, submission, question, selected_offering_id=offering)


async def test_a_text_answer_is_still_one_per_question(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The narrowed predicate still holds text answers to one per question."""
    _, question, submission = form
    await add_answer(db_engine, submission, question, answer_text="first")

    with pytest.raises(IntegrityError, match="ux_intake_answers_one_per_question"):
        await add_answer(db_engine, submission, question, answer_text="second")


@pytest.mark.parametrize(
    ("question_type", "source", "constraint"),
    [
        ("free_text", "help_areas", "only_choice_has_options_source"),
        ("multi_choice", "my_offerings", "options_source_is_known"),
    ],
)
async def test_an_options_source_is_known_and_only_on_a_choice(
    db_engine: AsyncEngine,
    form: tuple[UUID, UUID, UUID],
    question_type: str,
    source: str,
    constraint: str,
) -> None:
    session_type, _, _ = form
    question = await add_question(db_engine, session_type, question_type=question_type)

    with pytest.raises(IntegrityError, match=constraint):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_type_questions SET options_source = :s WHERE id = :q"),
                {"s": source, "q": question},
            )
