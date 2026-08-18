"""The intake stack's constraints, which are the whole of what this release is.

Four tables and no endpoints — the read and write surface arrives next. So every
guarantee here rests on the schema, and nothing in the gate sees most of it:
`alembic check` compares tables, columns, types and regular indexes, and is blind
to `CHECK` constraints, partial indexes and foreign-key actions.

**The deletion policy is the part worth testing hardest.** ADR 0013's rule is
cascade where the child is meaningless without its parent and records no
auditable fact, restrict where it is evidence. Applied here it produces an
asymmetry that looks like an inconsistency and is not: a question cascades from
its offering, and an *answer* restricts its question.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def add_question(
    engine: AsyncEngine,
    session_type: UUID,
    *,
    question_type: str = "free_text",
    text_: str = "What do you want to cover?",
) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO session_type_questions "
                    "(session_type_id, question_text, question_type) "
                    "VALUES (:t, :q, :k) RETURNING id"
                ),
                {"t": session_type, "q": text_, "k": question_type},
            )
        ).scalar_one()


async def add_option(engine: AsyncEngine, question: UUID, label: str = "Yes") -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO session_type_question_options (question_id, option_text) "
                    "VALUES (:q, :o) RETURNING id"
                ),
                {"q": question, "o": label},
            )
        ).scalar_one()


async def add_session(engine: AsyncEngine, mentor: UUID, session_type: UUID) -> tuple[UUID, UUID]:
    """A booked session and its mentee. Returns `(session_id, mentee_id)`."""
    async with engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mo', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{uuid4()}@example.test"},
            )
        ).scalar_one()
        session = (
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(mentor_id, mentee_id, session_type_id, starts_at, duration_minutes, status) "
                    "VALUES (:m, :e, :t, :s, 45, 'confirmed') RETURNING id"
                ),
                {
                    "m": mentor,
                    "e": mentee,
                    "t": session_type,
                    "s": dt.datetime.now(dt.UTC) + dt.timedelta(days=3),
                },
            )
        ).scalar_one()
    return session, mentee


async def add_submission(engine: AsyncEngine, session: UUID, mentee: UUID) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO intake_submissions (session_id, mentee_id) "
                    "VALUES (:s, :m) RETURNING id"
                ),
                {"s": session, "m": mentee},
            )
        ).scalar_one()


async def add_answer(
    engine: AsyncEngine, submission: UUID, question: UUID, **forms: object
) -> None:
    extra_columns = "".join(f", {key}" for key in forms)
    extra_values = "".join(f", :{key}" for key in forms)
    statement = (
        f"INSERT INTO intake_answers (submission_id, question_id{extra_columns}) "  # noqa: S608
        f"VALUES (:s, :q{extra_values})"
    )
    async with engine.begin() as conn:
        await conn.execute(text(statement), {"s": submission, "q": question, **forms})


@pytest_asyncio.fixture
async def form(db_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """A mentor's offering, one question on it, and a booked session's submission.

    Returns `(session_type, question, submission)`.
    """
    mentor = await make_public_mentor(db_engine, f"intake-{uuid4().hex[:8]}")
    session_type = await add_session_type(db_engine, mentor)
    question = await add_question(db_engine, session_type)
    session, mentee = await add_session(db_engine, mentor, session_type)
    submission = await add_submission(db_engine, session, mentee)
    return session_type, question, submission


# --------------------------------------------------------------------------
# exactly_one_answer_form
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "form_field",
    [
        {"answer_text": "Some prose"},
        {"file_storage_key": "intake/statement-of-purpose.pdf"},
    ],
)
async def test_each_answer_form_is_accepted_on_its_own(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID], form_field: dict[str, object]
) -> None:
    """The positive case, per form — asserted because a constraint that refused
    everything would pass every negative test below."""
    _, question, submission = form

    await add_answer(db_engine, submission, question, **form_field)


async def test_a_selected_option_is_accepted_on_its_own(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The third form, which needs an option row to point at — the reason
    `session_type_question_options` ships even though nothing writes it yet."""
    session_type, _, submission = form
    question = await add_question(
        db_engine, session_type, question_type="multi_choice", text_="Pick"
    )
    option = await add_option(db_engine, question)

    await add_answer(db_engine, submission, question, selected_option_id=option)


async def test_an_answer_carrying_nothing_is_refused(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """**The case a chain of `OR`s would permit**, and why the constraint sums.

    `answer_text IS NULL OR file_storage_key IS NULL OR selected_option_id IS
    NULL` is satisfied by all three being null — a row asserting a question was
    answered while holding no answer.
    """
    _, question, submission = form

    with pytest.raises(IntegrityError):
        await add_answer(db_engine, submission, question)


async def test_an_answer_carrying_two_forms_is_refused(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The other direction: a file upload that also carries prose leaves *which
    one is the answer* a question with two candidates and no rule."""
    _, question, submission = form

    with pytest.raises(IntegrityError):
        await add_answer(
            db_engine,
            submission,
            question,
            answer_text="prose",
            file_storage_key="intake/draft.pdf",
        )


# --------------------------------------------------------------------------
# Uniqueness
# --------------------------------------------------------------------------


async def test_a_session_has_at_most_one_submission(db_engine: AsyncEngine) -> None:
    """`UNIQUE (session_id)` — a 1:1 extension, with the invariant re-declared as
    a constraint because ADR 0015 makes the key a surrogate `id`.

    Losing it is silent: two forms for one booking, and whichever the reader
    joined first wins.
    """
    mentor = await make_public_mentor(db_engine, "one-submission")
    session_type = await add_session_type(db_engine, mentor)
    session, mentee = await add_session(db_engine, mentor, session_type)
    await add_submission(db_engine, session, mentee)

    with pytest.raises(IntegrityError):
        await add_submission(db_engine, session, mentee)


async def test_a_question_is_answered_once_per_submission(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """`UNIQUE (submission_id, question_id)`. Two answers to one question is the
    same failure one level down."""
    _, question, submission = form
    await add_answer(db_engine, submission, question, answer_text="first")

    with pytest.raises(IntegrityError):
        await add_answer(db_engine, submission, question, answer_text="second")


# --------------------------------------------------------------------------
# The vocabularies
# --------------------------------------------------------------------------


async def test_an_unknown_question_type_is_refused(db_engine: AsyncEngine) -> None:
    """`text` + `CHECK`, not a PostgreSQL enum (#100) — and the `CHECK` is what
    makes it closed for a writer that never touches Pydantic."""
    mentor = await make_public_mentor(db_engine, "bad-qtype")
    session_type = await add_session_type(db_engine, mentor)

    with pytest.raises(IntegrityError):
        await add_question(db_engine, session_type, question_type="rating")


async def test_an_unknown_intake_status_is_refused(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "bad-status")
    session_type = await add_session_type(db_engine, mentor)
    session, mentee = await add_session(db_engine, mentor, session_type)
    submission = await add_submission(db_engine, session, mentee)

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE intake_submissions SET status = 'approved' WHERE id = :s"),
                {"s": submission},
            )


async def test_a_new_submission_starts_as_a_draft(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The server default, and the reason the row exists before the mentee has
    finished: answers need somewhere to go while they are still typing."""
    _, _, submission = form

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, submitted_at FROM intake_submissions WHERE id = :s"),
                {"s": submission},
            )
        ).one()

    assert row.status == "draft"
    assert row.submitted_at is None


# --------------------------------------------------------------------------
# The deletion policy, and its one asymmetry
# --------------------------------------------------------------------------


async def test_deleting_an_offering_takes_its_questions(db_engine: AsyncEngine) -> None:
    """`CASCADE`, per ADR 0013: a question is meaningless without the offering
    that asks it and records no fact about anybody.

    A hard delete of a session type is not something the API can do — offerings
    are soft-deleted — so this asserts the rule rather than a reachable path.
    """
    mentor = await make_public_mentor(db_engine, "cascade-questions")
    session_type = await add_session_type(db_engine, mentor, config=False)
    await add_question(db_engine, session_type)

    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM session_types WHERE id = :t"), {"t": session_type})

    async with db_engine.connect() as conn:
        remaining = (
            await conn.execute(
                text("SELECT count(*) FROM session_type_questions WHERE session_type_id = :t"),
                {"t": session_type},
            )
        ).scalar_one()
    assert remaining == 0


async def test_a_question_with_answers_cannot_be_deleted(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """**The asymmetry, and it is deliberate.** The question cascades *from* its
    offering and is restricted *by* its answers.

    An answer is evidence of what was asked. Deleting the question out from under
    it would leave prose nobody can interpret — which is why
    `session_type_questions` carries `deleted_at`: retiring a question is how a
    mentor edits their form without being refused by every answer ever given.
    """
    _, question, submission = form
    await add_answer(db_engine, submission, question, answer_text="kept")

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM session_type_questions WHERE id = :q"), {"q": question}
            )


async def test_a_retired_question_keeps_its_answers(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """The path the restriction above leaves open, asserted so the pair is
    legible: soft-delete the question, and the answer survives pointing at it."""
    _, question, submission = form
    await add_answer(db_engine, submission, question, answer_text="kept")

    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_questions SET deleted_at = now() WHERE id = :q"),
            {"q": question},
        )

    async with db_engine.connect() as conn:
        answers = (
            await conn.execute(
                text("SELECT count(*) FROM intake_answers WHERE question_id = :q"), {"q": question}
            )
        ).scalar_one()
    assert answers == 1


async def test_an_option_in_use_cannot_be_deleted(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """`RESTRICT` on `selected_option_id`: which choice was made is evidence too,
    and it is the reason the options table needs no `deleted_at` of its own."""
    session_type, _, submission = form
    question = await add_question(
        db_engine, session_type, question_type="multi_choice", text_="Pick"
    )
    option = await add_option(db_engine, question)
    await add_answer(db_engine, submission, question, selected_option_id=option)

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM session_type_question_options WHERE id = :o"), {"o": option}
            )


async def test_deleting_a_submission_takes_its_answers(
    db_engine: AsyncEngine, form: tuple[UUID, UUID, UUID]
) -> None:
    """`CASCADE`: an answer belongs to the form it was given on, and has no
    meaning detached from it."""
    _, question, submission = form
    await add_answer(db_engine, submission, question, answer_text="goes with it")

    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM intake_submissions WHERE id = :s"), {"s": submission})

    async with db_engine.connect() as conn:
        answers = (
            await conn.execute(
                text("SELECT count(*) FROM intake_answers WHERE submission_id = :s"),
                {"s": submission},
            )
        ).scalar_one()
    assert answers == 0
