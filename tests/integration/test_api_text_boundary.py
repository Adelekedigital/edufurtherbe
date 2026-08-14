"""What this database cannot be given, and every door it arrives through.

A `str` Python accepts is not the set a PostgreSQL `text` column accepts, and the
gap has **two halves that fail in different places**:

- `U+0000` encodes to UTF-8 fine and the *server* then refuses the value —
  `CharacterNotInRepertoireError`.
- An unpaired surrogate never reaches the server at all: UTF-8 has no encoding
  for one, so *asyncpg* raises `UnicodeEncodeError` building the message.

Both are a **500 with a stack trace, on endpoints that take no token at all**, and
no gate sees either: the type is `str`, the value is bound rather than
interpolated, the SQL is correct, and both are legal Python.

**The second half was found by disproving the first.** This file once said NUL was
the only one, and the claim was probed rather than believed. It was wrong — which
is the argument for `storable()` being defined as *what cannot be stored* rather
than as a list of characters somebody thought of.

**Parametrised on purpose, against this suite's own precedent.** `test_api_writes`
argues that a decorator covering eight of nine endpoints reads as complete while
the ninth ships — and it is right, *because authorization is a separate guard per
endpoint*. Here the opposite holds: there is exactly one normaliser, and what
needs proving is that every door reaches it. A per-endpoint test would assert the
same clause thirteen times and still say nothing about the fourteenth door.

Every case asserts the **positive** result, not merely the absence of a 500. A
handler that swallowed the term and returned an empty page would pass a
status-code check and be a worse bug than the crash it replaced.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: Mid-word, so stripping it cannot be confused with trimming the ends — and so
#: a test asserting the cleaned result is asserting something specific.
NUL = "Love\x00lace"
URL_MENTORS = "/api/v1/mentors"


async def make_user(
    engine: AsyncEngine, auth_id: UUID, email: str, *, mentor: bool = False
) -> UUID:
    async with engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        if mentor:
            await conn.execute(
                text(
                    "INSERT INTO mentor_profiles (user_id, approval_status, listing_status) "
                    "VALUES (:u, 'approved', 'listed')"
                ),
                {"u": user},
            )
    return user


# --------------------------------------------------------------------------
# The three query parameters
# --------------------------------------------------------------------------


async def test_a_nul_in_a_mentor_search_finds_what_the_clean_term_finds(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Not just a 200 — the *same page* the byte-free term returns."""
    await make_bookable_mentor(db_engine, "nul-search")

    dirty = await api_client.get(URL_MENTORS, params={"q": NUL})
    clean = await api_client.get(URL_MENTORS, params={"q": "Lovelace"})

    assert dirty.status_code == 200, dirty.text
    assert [row["id"] for row in dirty.json()["data"]] == [
        row["id"] for row in clean.json()["data"]
    ]
    assert dirty.json()["data"], "the mentor is findable by this name at all"


async def test_a_term_that_is_only_a_nul_browses_rather_than_searching(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """It empties to nothing, and an empty box is the resting state of a search
    field — the same rule `?q=` and `?q=%20` already follow."""
    await make_bookable_mentor(db_engine, "nul-only")

    response = await api_client.get(f"{URL_MENTORS}?q=%00")

    assert response.status_code == 200, response.text
    assert response.json()["data"], "a blank search browses; it does not empty the directory"


async def test_a_nul_in_the_institution_autocomplete(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/api/v1/institutions", params={"q": NUL})

    assert response.status_code == 200, response.text


async def test_a_nul_in_a_lookup_filter_matches_what_the_clean_term_matches(
    api_client: httpx.AsyncClient,
) -> None:
    dirty = await api_client.get("/api/v1/catalog/countries", params={"q": "Nige\x00ria"})
    clean = await api_client.get("/api/v1/catalog/countries", params={"q": "Nigeria"})

    assert dirty.status_code == 200, dirty.text
    assert dirty.json()["data"] == clean.json()["data"]
    assert clean.json()["data"], "Nigeria is in the seeded catalogue"


# --------------------------------------------------------------------------
# The write paths — where the value is kept, not just matched
# --------------------------------------------------------------------------


async def test_a_nul_in_a_written_field_is_stored_without_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The row must exist and hold the cleaned text.

    A write is the half that matters most here: a search returning the wrong
    page is one bad response, and a stored value is wrong every time it is read
    afterwards — with nothing to compare it against.
    """
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, f"nul-write-{auth_id}@example.test")

    response = await api_client.post(
        f"/api/v1/users/{user}/awards",
        json={"title": NUL, "institution": "A Body"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201, response.text
    async with db_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT title FROM user_awards WHERE user_id = :u"), {"u": user}
            )
        ).scalar_one()
    assert stored == "Lovelace"


async def test_a_nul_in_the_one_write_schema_that_skipped_the_base(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`AvailabilityExceptionWrite.reason` inherits `_ZoneMixin`, not `Normalised`.

    It was the only free-text field in the API outside the shared base, so it was
    the only one where a fix to the base would not have reached — and it had
    never been trimmed either. Named rather than folded into the sweep above,
    because the reason it is separate is the reason it was missed.
    """
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, f"nul-reason-{auth_id}@example.test", mentor=True)

    response = await api_client.post(
        f"/api/v1/users/{user}/availability/exceptions",
        json={
            "type": "block",
            "start_date": "2027-01-04",
            "end_date": "2027-01-05",
            "timezone": "Africa/Lagos",
            "reason": NUL,
        },
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201, response.text
    async with db_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT reason FROM availability_exceptions WHERE mentor_user_id = :u"),
                {"u": user},
            )
        ).scalar_one()
    assert stored == "Lovelace"


# --------------------------------------------------------------------------
# The second cause, which fails somewhere else entirely
# --------------------------------------------------------------------------

#: A lone surrogate, as the six ASCII characters a client actually sends. Handing
#: `httpx` a Python `str` containing one does not work — httpx cannot encode it
#: either, and that failure is the test's, not the server's. JSON carries it as an
#: escape, so no encoding is needed anywhere along the way.
SURROGATE_BODY = (
    rb'{"type": "block", "start_date": "2027-03-01", "end_date": "2027-03-02",'
    rb' "timezone": "Africa/Lagos", "reason": "Love\ud800lace"}'
)


async def test_a_lone_surrogate_in_the_one_unconstrained_field(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The other half of "unstorable", and it fails **before** the database.

    UTF-8 has no encoding for an unpaired surrogate, so asyncpg raises
    `UnicodeEncodeError` while building the message — `DataError`, a 500, without
    PostgreSQL ever being asked. Different cause, different layer, same result as
    a NUL, which is why `storable()` is defined by what can be *stored* rather
    than by a list of characters.

    **Reachable through exactly one field.** A `str` with a length constraint
    makes pydantic-core parse the value as unicode, and it refuses a lone
    surrogate with a 422 before any of this matters — so every other free-text
    field in the API is already safe, by a mechanism nobody here chose.
    `AvailabilityExceptionWrite.reason` is the only one with no `max_length`, and
    it is the same field that skipped `Normalised`. One field being the odd one
    out twice, for two unrelated reasons, is the argument for the rule living in
    a base class rather than in a habit.
    """
    auth_id = uuid4()
    user = await make_user(db_engine, auth_id, f"surrogate-{auth_id}@example.test", mentor=True)

    response = await api_client.post(
        f"/api/v1/users/{user}/availability/exceptions",
        content=SURROGATE_BODY,
        headers={**bearer(api_token(auth_id)), "content-type": "application/json"},
    )

    assert response.status_code == 201, response.text
    async with db_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT reason FROM availability_exceptions WHERE mentor_user_id = :u"),
                {"u": user},
            )
        ).scalar_one()
    assert stored == "Lovelace"


async def test_a_length_constraint_refuses_a_surrogate_before_we_see_it() -> None:
    """Pins the upstream behaviour the paragraph above depends on.

    Deliberately **not** through an endpoint. Every request schema inherits
    `Normalised`, which cleans the value before field validation runs — so a test
    posting a surrogate to a real endpoint asserts our fix and says nothing about
    pydantic's, while reading as though it covered both. It has to be a bare model
    to be about anything.

    If pydantic-core ever stops parsing constrained strings as unicode, every
    free-text field in the API becomes a route to the 500 that only
    `AvailabilityExceptionWrite.reason` is today, and this is what would say so.
    Asserting somebody else's behaviour is legitimate when a change to it breaks
    us silently.
    """

    class Constrained(BaseModel):
        v: str = Field(min_length=1, max_length=300)

    class Unconstrained(BaseModel):
        v: str

    with pytest.raises(PydanticValidationError):
        Constrained.model_validate({"v": "Love\ud800lace"})

    # And the other half of the claim: without a constraint it sails through,
    # which is exactly why `reason` was the one field that could reach asyncpg.
    assert Unconstrained.model_validate({"v": "Love\ud800lace"}).v == "Love\ud800lace"


# --------------------------------------------------------------------------
# What must not change
# --------------------------------------------------------------------------


async def test_an_absent_parameter_still_filters_nothing(
    api_client: httpx.AsyncClient,
) -> None:
    """Sending no `q` at all must return the whole catalogue, not an error.

    **This replaced a test that asserted a non-invariant**, and a mutation is
    what exposed it. The first version claimed absent and `""` were "different
    from each other" and then asserted they were equal — and they are equal, at
    every one of these endpoints, so a cleaner that turned `None` into `""`
    changed nothing and the mutation survived.

    What is real is this: `lookup_page.q` defaults to `None`, and Pydantic does
    not run a validator on a default it was never given. So the normaliser only
    ever sees a `str` — which is why `StorableText` needs no `None` branch, and
    why the branch that was written first is gone. If that ever stops being true
    the validator receives `None`, `.replace` raises, and this endpoint 500s on
    its most ordinary request.
    """
    response = await api_client.get("/api/v1/catalog/countries")

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) > 1, "an absent filter is not a filter"
