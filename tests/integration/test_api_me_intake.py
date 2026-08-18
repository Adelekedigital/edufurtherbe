"""A mentor managing the form their offering asks.

**The five-question limit is the product rule with no column to hold it**, so it
is enforced in the store and tested here. The count is of *live* questions —
deleting one frees a slot, and counting every row would leave a mentor who added
and removed five stuck forever with an empty form.

**`multi_choice` is refused at the boundary.** The column accepts it and
`session_type_question_options` exists for its choices, but nothing can create an
option, so a `multi_choice` question would be one no mentee could answer. The
selectable set is narrower than the column's, the same shape as
`ConferencingProvider` against `MeetingProvider`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from app.domain.intake import MAX_QUESTIONS
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def url(session_type: UUID) -> str:
    return f"/api/v1/me/session-types/{session_type}/questions"


async def as_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    mentor = await make_public_mentor(engine, tag)
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return mentor, auth_id


def body(**overrides: object) -> dict[str, object]:
    return {"question_text": "What do you want to cover?"} | overrides


# --------------------------------------------------------------------------
# The form
# --------------------------------------------------------------------------


async def test_a_question_is_created_and_immediately_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "q-create")
    session_type = await add_session_type(db_engine, mentor)

    created = await api_client.post(
        url(session_type), json=body(is_required=True), headers=bearer(api_token(auth_id))
    )
    assert created.status_code == 201, created.text

    listed = await api_client.get(url(session_type), headers=bearer(api_token(auth_id)))
    (question,) = listed.json()["data"]
    assert question["id"] == created.json()["id"]
    assert question["question_type"] == "free_text"
    assert question["is_required"] is True


async def test_the_form_is_ordered_and_the_order_is_total(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`display_order` first, then creation order.

    The tiebreak matters because the column defaults to `0`: a mentor who never
    sets it would otherwise get whatever order the planner felt like, differing
    between requests on the same data.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-order")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))

    await api_client.post(
        url(session_type), json=body(question_text="B", display_order=2), headers=token
    )
    await api_client.post(
        url(session_type), json=body(question_text="A", display_order=1), headers=token
    )
    await api_client.post(url(session_type), json=body(question_text="C first"), headers=token)
    await api_client.post(url(session_type), json=body(question_text="C second"), headers=token)

    listed = await api_client.get(url(session_type), headers=token)

    assert [q["question_text"] for q in listed.json()["data"]] == [
        "C first",
        "C second",
        "A",
        "B",
    ]


async def test_a_paused_offerings_form_is_still_readable_and_editable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scope is `session_type_of()`, not the live predicate — preparing a
    paused offering is exactly when a mentor edits its form."""
    mentor, auth_id = await as_mentor(db_engine, "q-paused")
    session_type = await add_session_type(db_engine, mentor, active=False)

    created = await api_client.post(
        url(session_type), json=body(), headers=bearer(api_token(auth_id))
    )

    assert created.status_code == 201, created.text


# --------------------------------------------------------------------------
# The limit
# --------------------------------------------------------------------------


async def test_the_sixth_question_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A product rule with no column to hold it.**

    "At most five rows in a group" is not expressible as a `CHECK`, which sees
    one row, or as a unique index, which enforces distinctness rather than
    cardinality — so it is counted in the store and asserted here.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-limit")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))

    for index in range(MAX_QUESTIONS):
        accepted = await api_client.post(
            url(session_type), json=body(question_text=f"Q{index}"), headers=token
        )
        assert accepted.status_code == 201, accepted.text

    refused = await api_client.post(
        url(session_type), json=body(question_text="One too many"), headers=token
    )

    assert refused.status_code == 409, refused.text


async def test_deleting_a_question_frees_a_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The count is of live questions**, and this is the difference.

    Counting every row would leave a mentor who added and removed five stuck at
    the limit forever, with an empty form and nothing to explain it.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-frees")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))
    ids = []
    for index in range(MAX_QUESTIONS):
        created = await api_client.post(
            url(session_type), json=body(question_text=f"Q{index}"), headers=token
        )
        ids.append(created.json()["id"])

    await api_client.delete(f"{url(session_type)}/{ids[0]}", headers=token)
    accepted = await api_client.post(
        url(session_type), json=body(question_text="Replacement"), headers=token
    )

    assert accepted.status_code == 201, accepted.text


async def test_the_limit_is_per_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A full form on one offering must not block another.

    A count keyed on the mentor rather than the session type would do exactly
    that, and every other test here would still pass.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-per-offering")
    full = await add_session_type(db_engine, mentor, name="Full")
    spare = await add_session_type(db_engine, mentor, name="Spare")
    token = bearer(api_token(auth_id))
    for index in range(MAX_QUESTIONS):
        await api_client.post(url(full), json=body(question_text=f"Q{index}"), headers=token)

    accepted = await api_client.post(url(spare), json=body(), headers=token)

    assert accepted.status_code == 201, accepted.text


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


async def test_multi_choice_is_not_selectable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The column accepts it; the boundary does not.

    `session_type_question_options` exists and nothing can create an option, so
    a `multi_choice` question would be one no mentee could answer. The refusal
    lifts when option management arrives, and the message says what is missing
    rather than that the value is unknown.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-multi")
    session_type = await add_session_type(db_engine, mentor)

    refused = await api_client.post(
        url(session_type),
        json=body(question_type="multi_choice"),
        headers=bearer(api_token(auth_id)),
    )

    assert refused.status_code == 422, refused.text
    assert "option" in refused.text


async def test_an_unknown_question_type_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "q-unknown")
    session_type = await add_session_type(db_engine, mentor)

    refused = await api_client.post(
        url(session_type), json=body(question_type="rating"), headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 422, refused.text


# --------------------------------------------------------------------------
# Ownership, and what deletion keeps
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post"])
async def test_another_mentors_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, method: str
) -> None:
    """Scoped in the query on every operation, not checked afterwards."""
    _, auth_id = await as_mentor(db_engine, f"q-mine-{method}")
    other = await make_public_mentor(db_engine, f"q-theirs-{method}")
    theirs = await add_session_type(db_engine, other, name="Theirs")

    call = getattr(api_client, method)
    kwargs = {"json": body()} if method == "post" else {}
    refused = await call(url(theirs), headers=bearer(api_token(auth_id)), **kwargs)

    assert refused.status_code == 404, refused.text


async def test_a_question_cannot_be_edited_through_another_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Both ids are in the `WHERE`**, and this is what that buys.

    Scoping on `question_id` alone would let a mentor reach any question by
    naming one of their own offerings and somebody else's question id. The
    offering is what carries ownership, so the question is reached through it.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-cross")
    mine = await add_session_type(db_engine, mentor, name="Mine")
    other_mentor = await make_public_mentor(db_engine, "q-cross-other")
    theirs = await add_session_type(db_engine, other_mentor, name="Theirs")
    async with db_engine.begin() as conn:
        their_question = (
            await conn.execute(
                text(
                    "INSERT INTO session_type_questions "
                    "(session_type_id, question_text, question_type) "
                    "VALUES (:t, 'Theirs', 'free_text') RETURNING id"
                ),
                {"t": theirs},
            )
        ).scalar_one()

    refused = await api_client.patch(
        f"{url(mine)}/{their_question}",
        json={"question_text": "Mine now"},
        headers=bearer(api_token(auth_id)),
    )

    assert refused.status_code == 404, refused.text
    async with db_engine.connect() as conn:
        unchanged = (
            await conn.execute(
                text("SELECT question_text FROM session_type_questions WHERE id = :q"),
                {"q": their_question},
            )
        ).scalar_one()
    assert unchanged == "Theirs"


async def test_a_patch_touches_only_what_it_names(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent is not null; without `exclude_unset` a reword resets the rest."""
    mentor, auth_id = await as_mentor(db_engine, "q-patch")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))
    created = await api_client.post(
        url(session_type),
        json=body(question_text="Before", is_required=True, display_order=3),
        headers=token,
    )

    await api_client.patch(
        f"{url(session_type)}/{created.json()['id']}",
        json={"question_text": "After"},
        headers=token,
    )

    (question,) = (await api_client.get(url(session_type), headers=token)).json()["data"]
    assert question["question_text"] == "After"
    assert question["is_required"] is True
    assert question["display_order"] == 3


async def test_a_deleted_question_leaves_the_form_and_keeps_its_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Soft, because `intake_answers.question_id` restricts.

    A hard delete of an answered question is refused by the database, so a form
    that could never change once anybody filled it in would not be a form. The
    row stays and the read drops it.
    """
    mentor, auth_id = await as_mentor(db_engine, "q-delete")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))
    created = await api_client.post(url(session_type), json=body(), headers=token)
    question_id = created.json()["id"]

    removed = await api_client.delete(f"{url(session_type)}/{question_id}", headers=token)
    assert removed.status_code == 204, removed.text

    listed = await api_client.get(url(session_type), headers=token)
    assert listed.json()["data"] == []

    async with db_engine.connect() as conn:
        deleted_at = (
            await conn.execute(
                text("SELECT deleted_at FROM session_type_questions WHERE id = :q"),
                {"q": question_id},
            )
        ).scalar_one()
    assert deleted_at is not None


async def test_deleting_twice_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "q-twice")
    session_type = await add_session_type(db_engine, mentor)
    token = bearer(api_token(auth_id))
    created = await api_client.post(url(session_type), json=body(), headers=token)
    question_id = created.json()["id"]

    await api_client.delete(f"{url(session_type)}/{question_id}", headers=token)
    again = await api_client.delete(f"{url(session_type)}/{question_id}", headers=token)

    assert again.status_code == 404, again.text
