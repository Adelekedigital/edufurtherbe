"""People, bookings and messages for the Messages tests (ADR 0010).

Shared by `test_api_messages.py` and `test_api_message_reads.py`, so the two
files build their fixtures one way.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import add_user, api_token, bearer

ROOT = "/api/v1/me/messages"


@dataclass(frozen=True)
class Person:
    id: UUID
    headers: dict[str, str]


async def person(
    engine: AsyncEngine,
    tag: str,
    *,
    first_name: str | None = "Ada",
    last_name: str | None = None,
    slug: str | None = None,
    mentor: str | None = None,
) -> Person:
    """A user with a token. ``mentor`` gives them a mentor profile in that
    state: ``listed`` or ``unlisted`` (both approved), or ``pending``."""
    auth = uuid4()
    async with engine.begin() as conn:
        user = await add_user(
            conn,
            f"{tag}-{uuid4().hex[:8]}@example.test".lower(),
            auth_id=auth,
            first_name=first_name,
            last_name=last_name,
            slug=slug.lower().replace("_", "-") if slug else None,
            primary_role="mentor" if mentor else "mentee",
            timezone="UTC",
        )
        if mentor is not None:
            approval = "pending" if mentor == "pending" else "approved"
            listing = "listed" if mentor == "listed" else "unlisted"
            await conn.execute(
                text(
                    "INSERT INTO mentor_profiles (user_id, headline, approval_status, "
                    "listing_status) VALUES (:u, 'M', :a, :l)"
                ),
                {"u": user, "a": approval, "l": listing},
            )
    return Person(user, bearer(api_token(auth)))


async def book(
    engine: AsyncEngine,
    mentor: Person,
    mentee: Person,
    *,
    status: str = "confirmed",
    starts_in: dt.timedelta = dt.timedelta(days=3),
    booked_ago: dt.timedelta = dt.timedelta(days=1),
    type_name: str | None = "CV review",
) -> UUID:
    """One session between the two, ``starts_in`` from now, booked ``booked_ago``."""
    now = dt.datetime.now(dt.UTC)
    async with engine.begin() as conn:
        session_type = None
        if type_name is not None:
            session_type = (
                await conn.execute(
                    text("SELECT id FROM session_types WHERE mentor_user_id = :m AND name = :n"),
                    {"m": mentor.id, "n": type_name},
                )
            ).scalar_one_or_none() or (
                await conn.execute(
                    text(
                        "INSERT INTO session_types (mentor_user_id, name) VALUES (:m, :n) "
                        "RETURNING id"
                    ),
                    {"m": mentor.id, "n": type_name},
                )
            ).scalar_one()
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, "
                            "starts_at, duration_minutes, status, created_at) "
                            "VALUES (:m, :e, :t, :s, 45, :st, :c) RETURNING id"
                        ),
                        {
                            "m": mentor.id,
                            "e": mentee.id,
                            "t": session_type,
                            "s": now + starts_in,
                            "st": status,
                            "c": now - booked_ago,
                        },
                    )
                ).scalar_one()
            )
        )


async def pair(engine: AsyncEngine, tag: str, **booking: Any) -> tuple[Person, Person]:
    """A listed mentor and a mentee with one booking between them."""
    mentor = await person(
        engine, f"{tag}-mentor", first_name="Ada", slug=f"{tag}-ada", mentor="listed"
    )
    mentee = await person(engine, f"{tag}-mentee", first_name="Mo")
    await book(engine, mentor, mentee, **booking)
    return mentor, mentee


async def send(
    client: httpx.AsyncClient,
    sender: Person,
    to: Person | UUID,
    body: str = "Hello",
    *,
    client_id: UUID | None = None,
) -> httpx.Response:
    target = to.id if isinstance(to, Person) else to
    return await client.post(
        f"{ROOT}/{target}",
        json={"client_id": str(client_id or uuid4()), "body": body},
        headers=sender.headers,
    )


async def thread(
    client: httpx.AsyncClient, reader: Person, other: Person | UUID, **params: Any
) -> httpx.Response:
    target = other.id if isinstance(other, Person) else other
    return await client.get(f"{ROOT}/{target}", params=params, headers=reader.headers)


async def close_account(engine: AsyncEngine, who: Person) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": who.id})


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()
