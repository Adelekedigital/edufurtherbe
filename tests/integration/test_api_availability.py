"""The availability endpoints: refusals first, then behaviour.

**Written as refusals first because a write that leaks changes data**, and there
is no version to compare against afterwards. Every write endpoint gets its own
refusal test rather than a parametrised sweep — a decorator covering five of six
reads as complete, and the sixth is the one that ships.

The case that matters most is **another user's record id under the caller's own
URL**. The dependency passes there — the caller really is the owner of the URL —
so the `WHERE` clause in the store is the only thing left. A mutation batch found
nine endpoints in M2 whose refusal tests all stopped at the dependency and never
exercised that clause.

The asymmetry, same as the profile routes: **an admin may read these and may not
write them.** One clause of difference between `TargetUserDep` and `OwnerDep`,
and nothing but a test says which endpoint got which.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

RULE = {
    "day_of_week": 1,
    "start_time": "09:00",
    "end_time": "12:00",
    "timezone": "Africa/Lagos",
}
EXCEPTION = {
    "type": "block",
    "start_date": "2026-03-01",
    "end_date": "2026-03-02",
    "timezone": "Africa/Lagos",
}


async def make_mentor(
    engine: AsyncEngine, auth_id: UUID, email: str, *, admin: bool = False
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentor', 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
            {"u": user_id},
        )
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user_id},
            )
    return user_id


def url(user_id: UUID, suffix: str) -> str:
    return f"/api/v1/users/{user_id}/availability/{suffix}"


async def send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """One call site for every request in this file.

    `httpx`'s `delete()` takes no `json` argument at all — not even `None` — so
    passing one uniformly raises `TypeError` and every DELETE refusal test fails
    for a reason that has nothing to do with authorization. Branching here keeps
    that out of the tests themselves.
    """
    kwargs: dict[str, Any] = {"headers": headers} if headers else {}
    if body is not None and method != "delete":
        kwargs["json"] = body
    return await getattr(client, method)(path, **kwargs)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

#: Every write, as (method, suffix, body). An endpoint added without a refusal
#: test is a line missing from here, which is visible in a diff.
WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("post", "rules", RULE),
    ("patch", "rules/{id}", {"is_active": False}),
    ("delete", "rules/{id}", {}),
    ("post", "exceptions", EXCEPTION),
    ("delete", "exceptions/{id}", {}),
]


@pytest.mark.parametrize(("method", "suffix", "body"), WRITES, ids=[w[1] for w in WRITES])
async def test_a_write_without_a_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, method: str, suffix: str, body: Any
) -> None:
    owner = await make_mentor(db_engine, uuid4(), "owner@example.test")
    path = url(owner, suffix.replace("{id}", str(uuid4())))

    response = await send(api_client, method, path, body=body or None)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "suffix", "body"), WRITES, ids=[w[1] for w in WRITES])
async def test_a_write_to_another_users_url_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, method: str, suffix: str, body: Any
) -> None:
    owner = await make_mentor(db_engine, uuid4(), "owner2@example.test")
    intruder_auth = uuid4()
    await make_mentor(db_engine, intruder_auth, "intruder@example.test")
    path = url(owner, suffix.replace("{id}", str(uuid4())))

    response = await send(
        api_client, method, path, body=body or None, headers=bearer(api_token(intruder_auth))
    )

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "suffix", "body"), WRITES, ids=[w[1] for w in WRITES])
async def test_an_admin_may_not_write_somebody_elses_availability(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, method: str, suffix: str, body: Any
) -> None:
    """The asymmetry. An admin reviewing a mentor's schedule is a review; an
    admin silently editing it is an audit trail nobody has designed."""
    owner = await make_mentor(db_engine, uuid4(), "owner3@example.test")
    admin_auth = uuid4()
    await make_mentor(db_engine, admin_auth, "admin@example.test", admin=True)
    path = url(owner, suffix.replace("{id}", str(uuid4())))

    response = await send(
        api_client, method, path, body=body or None, headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("patch", "rules/{id}"), ("delete", "rules/{id}"), ("delete", "exceptions/{id}")],
)
async def test_another_users_record_id_under_your_own_url_changes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, method: str, suffix: str
) -> None:
    """**The case the dependency cannot catch.**

    The caller owns the URL, so `OwnerDep` passes — the only thing standing
    between them and somebody else's row is the `WHERE mentor_user_id = :user`
    clause in the store. Removing that clause leaves every other refusal test in
    this file green.
    """
    victim_auth, caller_auth = uuid4(), uuid4()
    victim = await make_mentor(db_engine, victim_auth, "victim@example.test")
    caller = await make_mentor(db_engine, caller_auth, "caller@example.test")

    created = await api_client.post(
        url(victim, "rules" if "rules" in suffix else "exceptions"),
        json=RULE if "rules" in suffix else EXCEPTION,
        headers=bearer(api_token(victim_auth)),
    )
    assert created.status_code == 201
    victims_row = created.json()["id"]

    response = await send(
        api_client,
        method,
        url(caller, suffix.replace("{id}", victims_row)),
        body={"is_active": False} if method == "patch" else None,
        headers=bearer(api_token(caller_auth)),
    )

    assert response.status_code == 404

    # And the victim's row is untouched — a 404 that still wrote would be worse
    # than a 200 that did.
    still_there = await api_client.get(
        url(victim, "rules" if "rules" in suffix else "exceptions"),
        headers=bearer(api_token(victim_auth)),
    )
    assert len(still_there.json()) == 1


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------


async def test_a_mentor_declares_and_reads_back_a_weekly_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "declare@example.test")

    created = await api_client.post(
        url(mentor, "rules"), json=RULE, headers=bearer(api_token(auth))
    )
    listed = await api_client.get(url(mentor, "rules"), headers=bearer(api_token(auth)))

    assert created.status_code == 201
    assert created.headers["Location"].endswith(created.json()["id"])
    body = listed.json()
    assert len(body) == 1
    # Wall clock plus zone, exactly as declared. Never an instant: a recurring
    # rule has not named a date, and converting one would bake in a single
    # date's UTC offset.
    assert body[0]["start_time"] == "09:00:00"
    assert body[0]["timezone"] == "Africa/Lagos"


async def test_split_availability_is_two_rules_on_one_weekday(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Morning and afternoon with a gap — the shape the legacy one-row-per-day
    structure could not hold, and the reason there is no unique constraint on
    `(mentor, day_of_week)`."""
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "split@example.test")

    first = await api_client.post(url(mentor, "rules"), json=RULE, headers=bearer(api_token(auth)))
    second = await api_client.post(
        url(mentor, "rules"),
        json={**RULE, "start_time": "14:00", "end_time": "17:00"},
        headers=bearer(api_token(auth)),
    )

    assert (first.status_code, second.status_code) == (201, 201)


async def test_an_overlapping_window_is_a_409_not_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The exclusion constraint reaches the API as an `IntegrityError`. Unmapped
    it is a 500 on an ordinary mentor mistake — dragging a window across a
    neighbour — which is the failure this mapping exists to prevent."""
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "overlap@example.test")
    await api_client.post(url(mentor, "rules"), json=RULE, headers=bearer(api_token(auth)))

    clash = await api_client.post(
        url(mentor, "rules"),
        json={**RULE, "start_time": "10:00", "end_time": "13:00"},
        headers=bearer(api_token(auth)),
    )

    assert clash.status_code == 409
    assert "overlaps" in clash.text


async def test_a_deleted_window_stops_blocking_its_own_hours(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Why the exclusion constraint is partial on `deleted_at`. Without that
    predicate a mentor could never replace a window with a different one
    covering the same hours — the old row would keep refusing the new one,
    invisibly, since it no longer renders anywhere."""
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "replace@example.test")
    created = await api_client.post(
        url(mentor, "rules"), json=RULE, headers=bearer(api_token(auth))
    )

    await api_client.delete(
        url(mentor, f"rules/{created.json()['id']}"), headers=bearer(api_token(auth))
    )
    again = await api_client.post(
        url(mentor, "rules"),
        json={**RULE, "start_time": "10:00", "end_time": "13:00"},
        headers=bearer(api_token(auth)),
    )

    assert again.status_code == 201


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("timezone", "Eastern Standard Time", "not an IANA name"),
        ("timezone", "GMT+1", "an offset, not a zone"),
        ("day_of_week", 7, "outside 0-6"),
        ("end_time", "09:00", "not after start_time"),
    ],
)
async def test_a_bad_rule_is_refused_at_the_boundary(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, field: str, value: Any, why: str
) -> None:
    """422 from the schema, not a constraint violation surfacing as a 500.

    The timezone cases matter most: the column is `text` with no CHECK, so this
    validator is the only thing between a request and a value that raises inside
    the projection when somebody's availability is rendered.
    """
    auth = uuid4()
    # From the uuid, not from the parameters: `users.email` carries
    # `ck_users_email_is_lowercase`, and "Eastern Standard Time" in an address
    # fails it — a fixture breaking on the thing it is not testing.
    mentor = await make_mentor(db_engine, auth, f"bad-{auth.hex[:8]}@example.test")

    response = await api_client.post(
        url(mentor, "rules"), json={**RULE, field: value}, headers=bearer(api_token(auth))
    )

    assert response.status_code == 422, why


async def test_an_admin_may_read_what_they_may_not_write(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, admin_auth = uuid4(), uuid4()
    mentor = await make_mentor(db_engine, auth, "readable@example.test")
    await make_mentor(db_engine, admin_auth, "reader@example.test", admin=True)
    await api_client.post(url(mentor, "rules"), json=RULE, headers=bearer(api_token(auth)))

    read = await api_client.get(url(mentor, "rules"), headers=bearer(api_token(admin_auth)))

    assert read.status_code == 200
    assert len(read.json()) == 1


async def test_an_exception_round_trips_with_an_exclusive_end_date(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`end_date` is exclusive, matching the `daterange [)` the column stores —
    so one blocked day spans `d` to `d + 1` and never overlaps the next."""
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "exception@example.test")

    created = await api_client.post(
        url(mentor, "exceptions"), json=EXCEPTION, headers=bearer(api_token(auth))
    )
    listed = await api_client.get(url(mentor, "exceptions"), headers=bearer(api_token(auth)))

    assert created.status_code == 201
    body = listed.json()[0]
    assert (body["start_date"], body["end_date"]) == ("2026-03-01", "2026-03-02")
    assert body["type"] == "block"


async def test_a_half_populated_time_pair_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A start with no end has no defensible reading — open-ended, or midnight?
    Refusing the pair is what keeps the projection from having to guess."""
    auth = uuid4()
    mentor = await make_mentor(db_engine, auth, "halfpair@example.test")

    response = await api_client.post(
        url(mentor, "exceptions"),
        json={**EXCEPTION, "start_time": "09:00"},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422
