"""A mentor's own calendar, subtracted from what a mentee may book.

**The two halves of `busy` are indistinguishable by the time they are
subtracted**, and that is the design: a commitment is a commitment whether this
platform booked it or Google merely knows about it. So the assertions here are
about the *seam* — that the read happens at all, that it happens once, that it
happens again inside the booking transaction, and that failing it costs nothing.

**Failing open is asserted as hard as failing correctly.** ADR 0004 calls
free/busy advisory and says it "must never be treated as the mechanism that
prevents double booking" — so an outage must degrade to declared availability,
not empty every connected mentor's calendar. A test that only proved the
subtraction works would let the opposite ship.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_slots import at, make_mentee, make_mentor

from app.core.config import Settings
from app.domain.availability import UtcInterval
from app.infra.clients.meetings import CalendarAccessRevokedError, VenueUnavailableError
from app.infra.clients.secrets import seal
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

KEY = Fernet.generate_key().decode()

#: Far enough ahead that no notice window reaches it.
DAY = dt.date.today() + dt.timedelta(days=7)


def configured() -> Settings:
    return Settings(
        _env_file=None,
        google_calendar_client_id="cid",
        google_calendar_client_secret="gcs",  # noqa: S106
        calendar_token_key=KEY,
        public_base_url="https://api.example.test",
    )


class FakeGoogle:
    """Stands in for the free/busy call, and records how often it was asked."""

    def __init__(self, busy: tuple[UtcInterval, ...] = (), raises: Exception | None = None) -> None:
        self.busy = busy
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[UtcInterval, ...]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.busy


def wire(client: httpx.AsyncClient, google: FakeGoogle | None) -> None:
    """Put settings and a mentor free/busy reader on `app.state`, as main.py would."""
    from app.infra.db.calendar_store import MentorFreeBusy, NullFreeBusy

    app = client._transport.app  # type: ignore[attr-defined]
    app.state.settings = configured()
    app.state.free_busy = (
        NullFreeBusy()
        if google is None
        else MentorFreeBusy(
            client_id="cid",
            client_secret="gcs",  # noqa: S106
            key=KEY,
            reader=google,
            # The app's own factory, which the test bound to its disposable
            # database — so the dead-grant write lands where the assertions look.
            session_factory=app.state.session_factory,
        )
    )


async def connect_calendar(engine: AsyncEngine, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calendar_connections "
                "(user_id, provider, refresh_token_encrypted, status) "
                "VALUES (:u, 'google', :t, 'active')"
            ),
            {"u": mentor, "t": seal("a-refresh-token", key=KEY)},
        )


async def connection_row(engine: AsyncEngine, mentor: UUID) -> dict[str, Any]:
    async with engine.begin() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT status, last_error, refresh_token_encrypted "
                        "FROM calendar_connections WHERE user_id = :u"
                    ),
                    {"u": mentor},
                )
            )
            .mappings()
            .one()
        )


def slots_url(mentor: UUID, session_type: UUID) -> str:
    return (
        f"/api/v1/users/{mentor}/availability/slots"
        f"?session_type_id={session_type}&start={DAY}&end={DAY + dt.timedelta(days=1)}"
    )


async def read_slots(
    client: httpx.AsyncClient, mentor: UUID, session_type: UUID
) -> list[dt.datetime]:
    response = await client.get(slots_url(mentor, session_type))
    assert response.status_code == 200, response.text
    return [dt.datetime.fromisoformat(slot["start"]) for slot in response.json()["data"]]


# --------------------------------------------------------------------------
# The subtraction
# --------------------------------------------------------------------------


async def test_a_mentors_own_commitment_is_not_offered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """09:00-12:00 Lagos at 45 minutes offers 08:00Z, 08:45Z, 09:30Z, 10:15Z.

    Google says the mentor is busy 08:45-09:30Z. That slot goes; the rest stay
    where they were, because **the grid is not re-anchored** — the same rule a
    booked session follows, and the reason a 45-minute window never starts
    offering 09:15.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-subtract")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle(busy=(UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),))
    wire(api_client, google)

    starts = await read_slots(api_client, mentor, session_type)

    assert at(DAY, "08:45") not in starts
    assert at(DAY, "08:00") in starts
    assert at(DAY, "10:15") in starts


async def test_a_commitment_ending_when_a_slot_starts_does_not_block_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`UtcInterval` is half-open, and Google's periods inherit that.

    A meeting ending at 09:30 and a slot starting at 09:30 are adjacent, not
    overlapping — the same `[)` that lets 09:00-12:00 and 12:00-14:00 touch in
    the exclusion constraint. Worth its own test because the intuitive reading
    is that the slot is consumed, and quietly dropping it would cost every
    connected mentor a bookable hour after each meeting.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-adjacent")
    await connect_calendar(db_engine, mentor)
    wire(api_client, FakeGoogle(busy=(UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),)))

    starts = await read_slots(api_client, mentor, session_type)

    assert at(DAY, "09:30") in starts


async def test_an_unconnected_mentor_costs_no_google_call(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Most mentors have connected nothing, and must stay byte-identical."""
    mentor, session_type = await make_mentor(db_engine, "fb-unconnected")
    google = FakeGoogle(busy=(UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),))
    wire(api_client, google)

    starts = await read_slots(api_client, mentor, session_type)

    assert google.calls == []
    assert at(DAY, "08:45") in starts


async def test_one_request_covers_the_whole_span(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Not one per day. This endpoint takes no token."""
    mentor, session_type = await make_mentor(db_engine, "fb-one-call")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle()
    wire(api_client, google)

    await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots"
        f"?session_type_id={session_type}&start={DAY}&end={DAY + dt.timedelta(days=56)}"
    )

    assert len(google.calls) == 1
    span = google.calls[0]
    assert (span["end"] - span["start"]).days >= 56


# --------------------------------------------------------------------------
# Failing open
# --------------------------------------------------------------------------


async def test_google_being_unreachable_leaves_the_slots_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An outage degrades to declared availability. It does not empty a calendar."""
    mentor, session_type = await make_mentor(db_engine, "fb-outage")
    await connect_calendar(db_engine, mentor)
    wire(api_client, FakeGoogle(raises=VenueUnavailableError("google is down")))

    starts = await read_slots(api_client, mentor, session_type)

    assert at(DAY, "08:00") in starts
    assert at(DAY, "08:45") in starts


async def test_a_transient_failure_does_not_cost_the_mentor_their_connection(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A rate limit must not become a re-consent."""
    mentor, session_type = await make_mentor(db_engine, "fb-transient")
    await connect_calendar(db_engine, mentor)
    wire(api_client, FakeGoogle(raises=VenueUnavailableError("429 rate limited")))

    await read_slots(api_client, mentor, session_type)

    row = await connection_row(db_engine, mentor)
    assert row["status"] == "active"
    assert row["last_error"] is None


# --------------------------------------------------------------------------
# A dead grant
# --------------------------------------------------------------------------


async def test_a_revoked_grant_is_recorded_and_stops_being_called(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """ADR 0004 names the absence of exactly this as a live gap.

    The write is on a read path, which is unusual and is safe because it is
    self-limiting: once the status leaves `active` the mentor is invisible to
    the token lookup, so the second request makes no call and no second write.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-revoked")
    await connect_calendar(db_engine, mentor)
    google = FakeGoogle(raises=CalendarAccessRevokedError("the grant was revoked"))
    wire(api_client, google)

    first = await read_slots(api_client, mentor, session_type)
    second = await read_slots(api_client, mentor, session_type)

    assert at(DAY, "08:00") in first
    assert at(DAY, "08:00") in second
    assert len(google.calls) == 1
    row = await connection_row(db_engine, mentor)
    assert row["status"] == "error"
    assert "revoked" in row["last_error"]
    # A grant Google has rejected is no longer a credential.
    assert row["refresh_token_encrypted"] == ""


async def test_the_mentor_can_see_why_it_stopped(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`GET /me/calendar` reads `null` once the grant is dead.

    **Which is a gap, stated rather than hidden.** The row survives with its
    `last_error`, so support can see what happened, but the mentor is shown
    nothing to distinguish "you never connected" from "your grant was revoked
    and we stopped reading". Closing that means `active_connection` returning
    errored rows too, which changes what `/me/calendar` means — a contract
    change belonging with the connection-health work ADR 0004 asks for, not
    smuggled into the read that discovered it.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-visible")
    await connect_calendar(db_engine, mentor)
    wire(api_client, FakeGoogle(raises=CalendarAccessRevokedError("the grant was revoked")))
    auth_id = uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )

    await read_slots(api_client, mentor, session_type)
    response = await api_client.get("/api/v1/me/calendar", headers=bearer(api_token(auth_id)))

    assert response.status_code == 200
    assert response.json() is None


# --------------------------------------------------------------------------
# The booking write, which is the one that matters
# --------------------------------------------------------------------------


async def book_at(
    client: httpx.AsyncClient, session_type: UUID, mentee_auth: UUID, when: dt.datetime
) -> httpx.Response:
    return await client.post(
        "/api/v1/sessions",
        headers=bearer(api_token(mentee_auth)) | {"Idempotency-Key": str(uuid4())},
        json={"session_type_id": str(session_type), "starts_at": when.isoformat()},
    )


async def a_mentee_for(engine: AsyncEngine, tag: str) -> UUID:
    mentee = await make_mentee(engine, tag)
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentee}
        )
    return auth_id


async def test_booking_over_a_google_conflict_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Checked inside the booking transaction, not only at slot render.**

    Free/busy is eventually consistent, so a grid built seconds ago can miss a
    conflict written since. This is the last look before the write, and it comes
    free because booking already asks `list_slots` for legality rather than
    re-deriving it.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-book-no", notice=0)
    await connect_calendar(db_engine, mentor)
    mentee_auth = await a_mentee_for(db_engine, "fb-book-no")
    wire(api_client, FakeGoogle(busy=(UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),)))

    response = await book_at(api_client, session_type, mentee_auth, at(DAY, "08:45"))

    assert response.status_code == 422
    assert "not available" in response.text


async def test_booking_a_free_instant_still_works(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await make_mentor(db_engine, "fb-book-yes", notice=0)
    await connect_calendar(db_engine, mentor)
    mentee_auth = await a_mentee_for(db_engine, "fb-book-yes")
    wire(api_client, FakeGoogle(busy=(UtcInterval(at(DAY, "08:45"), at(DAY, "09:30")),)))

    response = await book_at(api_client, session_type, mentee_auth, at(DAY, "08:00"))

    assert response.status_code == 201, response.text


async def test_an_outage_does_not_stop_a_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole point of failing open, stated where it is riskiest.

    Refusing here would make a third party's bad afternoon an outage for this
    platform, and would promote an advisory check to an authoritative one.
    """
    mentor, session_type = await make_mentor(db_engine, "fb-book-outage", notice=0)
    await connect_calendar(db_engine, mentor)
    mentee_auth = await a_mentee_for(db_engine, "fb-book-outage")
    wire(api_client, FakeGoogle(raises=VenueUnavailableError("google is down")))

    response = await book_at(api_client, session_type, mentee_auth, at(DAY, "08:45"))

    assert response.status_code == 201, response.text
