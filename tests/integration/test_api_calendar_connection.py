"""A mentor connecting the calendar the platform reads their busy hours from.

**The credential is the point of this file.** Everything here is one endpoint
group, but the thing being protected is the only secret this service stores on
somebody else's behalf — so the assertions are about what is in the database
after each call, not only about what came back on the wire.

**The forged-state test is the load-bearing one.** Google redirects a browser to
the callback, so it carries no bearer token; the mentor's identity travels in a
sealed `state` we issued. Without that seal an attacker could complete their own
Google consent against a victim's session and attach *their* calendar to
somebody else's account — a real account-takeover shape, not a theoretical one.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor

from app.core.config import Settings
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.clients.secrets import sealed_value, unseal
from conftest import PROBLEM_JSON, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/calendar"
CALLBACK = "/api/v1/callbacks/google/calendar"
KEY = Fernet.generate_key().decode()
REFRESH_TOKEN = "not-ciphertext-and-not-a-real-token"  # noqa: S105


def configured(**overrides: Any) -> Settings:
    """Settings with the calendar connection switched on."""
    values: dict[str, Any] = {
        "google_calendar_client_id": "cid.apps.googleusercontent.com",
        "google_calendar_client_secret": "gcs-secret",
        "calendar_token_key": KEY,
        "public_base_url": "https://api.example.test",
    }
    return Settings(_env_file=None, **(values | overrides))


class FakeExchange:
    """Stands in for the call to Google, and records what it was asked."""

    def __init__(self, tokens: dict[str, Any] | None = None) -> None:
        self.tokens = tokens if tokens is not None else {"refresh_token": REFRESH_TOKEN}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.tokens


class Refusing:
    """The real adapter's behaviour when Google sends back no refresh token."""

    def __call__(self, **_kwargs: Any) -> dict[str, Any]:
        raise VenueUnavailableError("Google returned no refresh token")


class FakeNaming:
    """Stands in for Google's userinfo call, and records the token it was given."""

    def __init__(self, email: str | None = "mentor@example.com") -> None:
        self.email = email
        self.calls: list[str] = []

    def __call__(self, *, access_token: str) -> str | None:
        self.calls.append(access_token)
        return self.email


def wire(
    client: httpx.AsyncClient,
    *,
    settings: Settings,
    exchange: Any = None,
    naming: Any = None,
) -> FakeExchange:
    """Put the settings and the fakes on `app.state`, as main.py would.

    **`naming` is left unset by most tests on purpose.** The default
    `FakeExchange` returns no `access_token`, and the dependency makes no
    userinfo call without one — so a test that does not care about the account's
    name reaches no network, and that is asserted rather than assumed by
    `test_no_access_token_means_no_call_to_google`.
    """
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.settings = settings
    fake = exchange if exchange is not None else FakeExchange()
    app.state.calendar_exchange = fake
    if naming is not None:
        app.state.calendar_account_email = naming
    return fake


async def a_mentor(engine: AsyncEngine, tag: str) -> tuple[UUID, str]:
    mentor = await make_public_mentor(engine, tag)
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return mentor, api_token(auth_id)


async def rows_for(engine: AsyncEngine, mentor: UUID) -> list[dict[str, Any]]:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT status, refresh_token_encrypted, external_account_id, "
                "external_account_email, last_error "
                "FROM calendar_connections WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": mentor},
        )
        return [dict(row) for row in result.mappings()]


async def connect_through_google(client: httpx.AsyncClient, token: str) -> httpx.Response:
    """Walk the real two-leg flow: ask for the consent URL, then come back."""
    started = await client.get(f"{URL}/connect", headers=bearer(token))
    assert started.status_code == 200, started.text
    state = httpx.URL(started.json()["consent_url"]).params["state"]
    return await client.get(CALLBACK, params={"code": "the-code", "state": state})


# --------------------------------------------------------------------------
# Reading the connection
# --------------------------------------------------------------------------


async def test_a_mentor_who_has_never_connected_reads_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await a_mentor(db_engine, "cal-none")
    wire(api_client, settings=configured())

    response = await api_client.get(URL, headers=bearer(token))

    assert response.status_code == 200
    assert response.json() is None


async def test_reading_the_connection_needs_a_token(api_client: httpx.AsyncClient) -> None:
    wire(api_client, settings=configured())

    response = await api_client.get(URL)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


# --------------------------------------------------------------------------
# Starting the consent
# --------------------------------------------------------------------------


async def test_the_consent_url_names_this_mentor_in_a_sealed_state(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, token = await a_mentor(db_engine, "cal-start")
    wire(api_client, settings=configured())

    response = await api_client.get(f"{URL}/connect", headers=bearer(token))

    assert response.status_code == 200
    state = httpx.URL(response.json()["consent_url"]).params["state"]
    # Sealed, not merely encoded: the mentor's id must not be readable from the
    # URL, and must not be changeable by whoever holds it.
    assert str(mentor) not in state
    assert json.loads(unseal(state, key=KEY)) == {"user_id": str(mentor)}


async def test_the_redirect_matches_the_one_the_exchange_will_send(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Google compares the two and refuses the pair when they differ.

    Two strings that happen to agree would fail only in production, and only for
    the first mentor to try — so the agreement is asserted rather than assumed.
    """
    _, token = await a_mentor(db_engine, "cal-redirect")
    exchange = wire(api_client, settings=configured())

    started = await api_client.get(f"{URL}/connect", headers=bearer(token))
    at_consent = httpx.URL(started.json()["consent_url"]).params["redirect_uri"]
    await connect_through_google(api_client, token)

    assert at_consent == f"https://api.example.test{CALLBACK}"
    assert exchange.calls[0]["redirect_uri"] == at_consent


async def test_an_unconfigured_deployment_refuses_rather_than_half_working(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """All four settings or none. A partial flow fails somewhere further down."""
    _, token = await a_mentor(db_engine, "cal-unconfigured")
    wire(api_client, settings=configured(calendar_token_key=None))

    response = await api_client.get(f"{URL}/connect", headers=bearer(token))

    assert response.status_code == 500
    # An operator fault, so the detail is withheld — it would name settings.
    assert "calendar_token_key" not in response.text


# --------------------------------------------------------------------------
# Completing the consent
# --------------------------------------------------------------------------


async def test_completing_the_consent_stores_an_encrypted_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, token = await a_mentor(db_engine, "cal-connect")
    wire(api_client, settings=configured())

    response = await connect_through_google(api_client, token)

    assert response.status_code == 200
    assert response.json() == {"connected": str(mentor)}
    (row,) = await rows_for(db_engine, mentor)
    assert row["status"] == "active"
    # The assertion the column name exists for: what is stored is not the token.
    assert row["refresh_token_encrypted"] != REFRESH_TOKEN
    assert REFRESH_TOKEN not in row["refresh_token_encrypted"]
    assert unseal(row["refresh_token_encrypted"], key=KEY) == REFRESH_TOKEN


async def test_the_connection_is_then_readable_and_carries_no_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await a_mentor(db_engine, "cal-read")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)

    response = await api_client.get(URL, headers=bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["connected_at"] is not None
    assert body["last_error"] is None
    # Not "no token field is populated" — no field that could carry one exists.
    assert REFRESH_TOKEN not in response.text
    assert not any("token" in key for key in body)


async def test_a_state_we_did_not_issue_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The CSRF control, stated as a test.

    A `state` sealed with somebody else's key names a mentor of the attacker's
    choosing. Accepting it would attach the attacker's calendar to that mentor's
    account.
    """
    mentor, _ = await a_mentor(db_engine, "cal-forged")
    wire(api_client, settings=configured())
    forged = sealed_value({"user_id": str(mentor)}, key=Fernet.generate_key().decode())

    response = await api_client.get(CALLBACK, params={"code": "c", "state": forged})

    assert response.status_code == 401
    assert await rows_for(db_engine, mentor) == []


async def test_a_state_that_is_merely_a_user_id_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The shape somebody reaches for when the seal looks like ceremony."""
    mentor, _ = await a_mentor(db_engine, "cal-plain")
    wire(api_client, settings=configured())

    response = await api_client.get(CALLBACK, params={"code": "c", "state": str(mentor)})

    assert response.status_code == 401
    assert await rows_for(db_engine, mentor) == []


async def test_a_callback_with_no_consent_on_it_is_refused(
    api_client: httpx.AsyncClient,
) -> None:
    """What a mentor who declines at Google's screen sends back."""
    wire(api_client, settings=configured())

    response = await api_client.get(CALLBACK, params={"error": "access_denied"})

    assert response.status_code == 422


async def test_google_refusing_the_consent_stores_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """What the real adapter raises when the grant was not fresh.

    A 200 carrying only an access token is Google's answer to a consent that was
    not fresh, and taking it would ship a connection that works for an hour and
    then stops with nothing saying why.
    """
    mentor, token = await a_mentor(db_engine, "cal-no-refresh")
    wire(api_client, settings=configured(), exchange=Refusing())

    response = await connect_through_google(api_client, token)

    assert response.status_code == 502
    assert await rows_for(db_engine, mentor) == []


async def test_a_token_response_of_an_unexpected_shape_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Not the adapter's guard — the one behind it.

    The exchange is swappable through `app.state`, so the endpoint cannot assume
    a well-formed response. Indexing the dict directly turned this into a 500
    `KeyError`; this test is why it does not.
    """
    mentor, token = await a_mentor(db_engine, "cal-odd-shape")
    wire(api_client, settings=configured(), exchange=FakeExchange({"access_token": "at"}))

    response = await connect_through_google(api_client, token)

    assert response.status_code == 502
    assert await rows_for(db_engine, mentor) == []


# --------------------------------------------------------------------------
# Reconnecting and disconnecting
# --------------------------------------------------------------------------


async def test_reconnecting_replaces_the_grant_rather_than_colliding(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor switching Google accounts. A plain insert would `409` here."""
    mentor, token = await a_mentor(db_engine, "cal-again")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)

    second = FakeExchange({"refresh_token": "the-second-token"})
    wire(api_client, settings=configured(), exchange=second)
    response = await connect_through_google(api_client, token)

    assert response.status_code == 200
    (row,) = await rows_for(db_engine, mentor)
    assert unseal(row["refresh_token_encrypted"], key=KEY) == "the-second-token"


async def test_disconnecting_destroys_the_credential(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A disconnect that left us able to read the calendar is not one."""
    mentor, token = await a_mentor(db_engine, "cal-disconnect")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)

    response = await api_client.delete(URL, headers=bearer(token))

    assert response.status_code == 204
    (row,) = await rows_for(db_engine, mentor)
    assert row["status"] == "revoked"
    assert row["refresh_token_encrypted"] == ""


async def test_a_disconnected_mentor_reads_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await a_mentor(db_engine, "cal-gone")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)
    await api_client.delete(URL, headers=bearer(token))

    response = await api_client.get(URL, headers=bearer(token))

    assert response.status_code == 200
    assert response.json() is None


async def test_disconnecting_nothing_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await a_mentor(db_engine, "cal-nothing")
    wire(api_client, settings=configured())

    response = await api_client.delete(URL, headers=bearer(token))

    assert response.status_code == 404


async def test_disconnecting_twice_is_a_404_rather_than_a_second_revoke(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, token = await a_mentor(db_engine, "cal-twice")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)
    await api_client.delete(URL, headers=bearer(token))

    response = await api_client.delete(URL, headers=bearer(token))

    assert response.status_code == 404
    assert len(await rows_for(db_engine, mentor)) == 1


async def test_reconnecting_after_a_disconnect_leaves_the_history(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The partial index is what makes this possible, and the point of it.

    A full unique index would refuse the reconnection; deleting on disconnect
    would lose the record that a connection ever existed.
    """
    mentor, token = await a_mentor(db_engine, "cal-rehistory")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, token)
    await api_client.delete(URL, headers=bearer(token))

    wire(api_client, settings=configured(), exchange=FakeExchange({"refresh_token": "third"}))
    response = await connect_through_google(api_client, token)

    assert response.status_code == 200
    revoked, active = await rows_for(db_engine, mentor)
    assert revoked["status"] == "revoked"
    assert revoked["refresh_token_encrypted"] == ""
    assert active["status"] == "active"
    assert unseal(active["refresh_token_encrypted"], key=KEY) == "third"


# --------------------------------------------------------------------------
# One mentor's grant is not another's
# --------------------------------------------------------------------------


async def test_one_mentor_cannot_read_or_revoke_another_mentors_grant(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Every statement is scoped to the caller, on read and on write."""
    owner, owner_token = await a_mentor(db_engine, "cal-owner")
    _, other_token = await a_mentor(db_engine, "cal-other")
    wire(api_client, settings=configured())
    await connect_through_google(api_client, owner_token)

    seen = await api_client.get(URL, headers=bearer(other_token))
    revoked = await api_client.delete(URL, headers=bearer(other_token))

    assert seen.json() is None
    assert revoked.status_code == 404
    (row,) = await rows_for(db_engine, owner)
    assert row["status"] == "active"


# --------------------------------------------------------------------------
# Naming the connected account (#180, ADR 0012 as amended 2026-10-06)
#
# A mentor who authorised the wrong Google account could not tell: the page said
# "Connected" and nothing else. `calendar.freebusy` cannot answer which account
# granted it — `calendarList.list` is outside the scope — so the consent was
# widened to `openid email` at the one moment it was free, before any mentor had
# connected anything on any environment.
# --------------------------------------------------------------------------


async def test_completing_the_consent_stores_and_returns_the_account_s_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole point: the mentor can see *which* account is connected."""
    mentor, token = await a_mentor(db_engine, "names-the-account")
    naming = FakeNaming("chosen@gmail.test")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange({"refresh_token": REFRESH_TOKEN, "access_token": "at-1"}),
        naming=naming,
    )

    assert (await connect_through_google(api_client, token)).status_code == 200

    # Read with the access token the consent produced, not the refresh token —
    # sending the long-lived credential to a userinfo endpoint would be a
    # needless widening of where it travels.
    assert naming.calls == ["at-1"]
    assert [row["external_account_email"] for row in await rows_for(db_engine, mentor)] == [
        "chosen@gmail.test"
    ]

    shown = await api_client.get(URL, headers=bearer(token))
    assert shown.json()["account_email"] == "chosen@gmail.test"


async def test_an_unnamed_account_still_connects(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The grant outranks its label.**

    A mentor completed a consent and Google issued a refresh token: the calendar
    is connected. Refusing because the address could not be read would answer
    `502` for something that worked, and tell them nothing was connected while
    it was.

    Watched to fail by raising from the lookup instead of returning `None`.
    """
    mentor, token = await a_mentor(db_engine, "unnamed-account")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange({"refresh_token": REFRESH_TOKEN, "access_token": "at-2"}),
        naming=FakeNaming(None),
    )

    assert (await connect_through_google(api_client, token)).status_code == 200

    rows = await rows_for(db_engine, mentor)
    assert [row["status"] for row in rows] == ["active"]
    assert rows[0]["refresh_token_encrypted"]
    assert rows[0]["external_account_email"] is None

    shown = await api_client.get(URL, headers=bearer(token))
    assert shown.status_code == 200
    assert shown.json()["status"] == "active"
    # Null, so a client degrades to "Connected" rather than showing "None".
    assert shown.json()["account_email"] is None


async def test_reconnecting_with_another_account_replaces_the_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Switching accounts is the reason this field exists.**

    Carrying the first address forward would label the new grant with the old
    account — the exact confusion naming it was meant to end, made worse by
    looking authoritative.
    """
    mentor, token = await a_mentor(db_engine, "switches-account")
    first = FakeNaming("wrong@gmail.test")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange({"refresh_token": REFRESH_TOKEN, "access_token": "at-3"}),
        naming=first,
    )
    assert (await connect_through_google(api_client, token)).status_code == 200

    api_client._transport.app.state.calendar_account_email = (  # type: ignore[attr-defined]
        FakeNaming("right@gmail.test")
    )
    assert (await connect_through_google(api_client, token)).status_code == 200

    rows = await rows_for(db_engine, mentor)
    assert [row["external_account_email"] for row in rows] == ["right@gmail.test"]
    assert (await api_client.get(URL, headers=bearer(token))).json()[
        "account_email"
    ] == "right@gmail.test"


async def test_no_access_token_means_no_call_to_google(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Asserted, not assumed**, because the alternative is a live network call.

    Every other test here completes a consent through a `FakeExchange` that
    returns no `access_token`. If the dependency called userinfo anyway it would
    reach `openidconnect.googleapis.com` from the test suite — passing, because
    the failure answers `None`, while quietly depending on the network.
    """
    mentor, token = await a_mentor(db_engine, "no-access-token")
    naming = FakeNaming("never-asked@gmail.test")
    wire(api_client, settings=configured(), naming=naming)

    assert (await connect_through_google(api_client, token)).status_code == 200

    assert naming.calls == []
    assert [row["external_account_email"] for row in await rows_for(db_engine, mentor)] == [None]


async def test_one_mentor_cannot_see_another_mentors_account_email(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The field is somebody's email address, so the scoping is the test.

    `active_connection` filters by `user_id` in the query rather than after the
    fetch, which is what makes this true for every column it selects — the new
    one included.
    """
    theirs, _ = await a_mentor(db_engine, "owns-the-account")
    mine, my_token = await a_mentor(db_engine, "other-mentor")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange({"refresh_token": REFRESH_TOKEN, "access_token": "at-4"}),
        naming=FakeNaming("private@gmail.test"),
    )
    _, their_token = await a_mentor(db_engine, "owns-the-account-again")
    assert (await connect_through_google(api_client, their_token)).status_code == 200

    mine_shown = await api_client.get(URL, headers=bearer(my_token))
    assert mine_shown.status_code == 200
    assert mine_shown.json() is None
    assert "private@gmail.test" not in mine_shown.text
    del theirs, mine


async def test_a_consent_without_the_calendar_permission_stores_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The grant Google's own consent screen makes easiest to produce.**

    Ticking the account and not the calendar checkbox yields `openid email`
    alone. Stored, it is a connection nothing can ever report as broken: the
    free/busy read fails `403`, which is transient, so `record_failure` is never
    reached and the sweep never marks it.

    `422` and no row, so the mentor is told now rather than never.
    """
    mentor, token = await a_mentor(db_engine, "withheld-calendar")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange(
            {
                "refresh_token": REFRESH_TOKEN,
                "access_token": "at-5",
                "scope": "email https://www.googleapis.com/auth/userinfo.email openid",
            }
        ),
        naming=FakeNaming("ticked-email-only@gmail.test"),
    )

    answered = await connect_through_google(api_client, token)

    assert answered.status_code == 422, answered.text
    assert answered.headers["content-type"].startswith(PROBLEM_JSON)
    assert await rows_for(db_engine, mentor) == []
    # Nothing to read, rather than a connection that cannot work.
    assert (await api_client.get(URL, headers=bearer(token))).json() is None


async def test_the_scope_google_sent_on_a_real_consent_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Guards the guard, with the string from the live consent of 2026-10-06.

    A membership test written against *our* request would pass while refusing
    what Google really sends: it expands `email` into `userinfo.email`, keeps
    both, and orders them its own way.
    """
    mentor, token = await a_mentor(db_engine, "real-granted-scope")
    wire(
        api_client,
        settings=configured(),
        exchange=FakeExchange(
            {
                "refresh_token": REFRESH_TOKEN,
                "access_token": "at-6",
                "scope": (
                    "email https://www.googleapis.com/auth/userinfo.email openid "
                    "https://www.googleapis.com/auth/calendar.freebusy"
                ),
            }
        ),
        naming=FakeNaming("real@gmail.test"),
    )

    assert (await connect_through_google(api_client, token)).status_code == 200
    rows = await rows_for(db_engine, mentor)
    assert [(r["status"], r["external_account_email"]) for r in rows] == [
        ("active", "real@gmail.test")
    ]
