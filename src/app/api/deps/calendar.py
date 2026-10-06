"""The mentor's Google Calendar connection and free/busy."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request

from app.api.deps.core import CurrentUserDep, SessionDep, _configured, get_session_factory
from app.api.schemas.conferencing import ConferencingWrite
from app.core.errors import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    ValidationError,
)
from app.infra.clients.meetings import (
    VenueUnavailableError,
    account_email,
    consent_url,
    exchange_code,
    require_calendar_scope,
)
from app.infra.clients.secrets import SealError, seal, sealed_value, unsealed_value
from app.infra.db.calendar_store import (
    active_connection,
    connect,
    disconnect,
    free_busy_reader,
)
from app.infra.db.conferencing_store import own_default_option, set_default_option

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.

#: Where Google sends a mentor back. One constant, because the value is sent to
#: Google on the consent request **and** again on the exchange, and Google
#: refuses the pair if they differ — two strings that happen to agree would fail
#: only in production, and only for the first mentor to try.
CALENDAR_REDIRECT_PATH = "/api/v1/callbacks/google/calendar"


def _calendar_oauth(request: Request) -> tuple[str, str, str, str]:
    """The client, the secret, the redirect and the sealing key, or a refusal.

    All four or none. A consent flow missing any one of them fails somewhere
    downstream with a message about whichever piece it happened to reach first,
    and an operator then debugs the symptom.
    """
    settings = _configured(request)
    base = settings.public_base_url
    if not (
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.calendar_token_key
        and base
    ):
        raise ConfigurationError("calendar connection is not configured")
    return (
        settings.google_calendar_client_id,
        settings.google_calendar_client_secret.get_secret_value(),
        f"{base.rstrip('/')}{CALENDAR_REDIRECT_PATH}",
        settings.calendar_token_key.get_secret_value(),
    )


def _free_busy(request: Request) -> Any:
    """The mentor's external calendar, or a reader that subtracts nothing.

    **Null unless all three settings are present**, which is the same shape
    `_calendar` uses and for the same reason: a deployment part-way through
    being configured should behave like one that has not started, not fail every
    slot render with an OAuth error. Unconnected mentors are unaffected either
    way — the reader checks for a grant before it calls anything.

    Read off `app.state` first so a test can wire a fake, following `_rooms`.
    """
    wired = getattr(request.app.state, "free_busy", None)
    if wired is not None:
        return wired
    return free_busy_reader(
        _configured(request),
        # **Its own session for the one write it makes.** A dead grant has to be
        # recorded whether the surrounding read commits or the surrounding
        # booking rolls back, and it must never commit either of them.
        getattr(request.app.state, "session_factory", None) or get_session_factory(),
    )


def _named_account(request: Request, tokens: dict[str, Any]) -> str | None:
    """The connected account's email, or ``None`` without reaching Google.

    **No access token means no call.** Google always returns one beside the
    refresh token, so an absent one is a stubbed exchange or a response shape we
    do not recognise — and calling userinfo with an empty bearer would be an
    outbound request that can only fail. It would also put a real network call
    inside every test that completes a consent through a fake exchange.
    """
    access = str(tokens.get("access_token") or "")
    if not access:
        return None
    found: str | None = _name_of(request)(access_token=access)
    return found


def _name_of(request: Request) -> Any:
    """The call that names the connected Google account.

    Read off `app.state` rather than called directly, following `_token_exchange`
    for the same reason: this is an outbound call whose *request* has to be right
    — a bearer token on Google's userinfo endpoint — and a seam is what lets a
    test assert what was sent rather than what came back.
    """
    return getattr(request.app.state, "calendar_account_email", None) or account_email


def _token_exchange(request: Request) -> Any:
    """The call that turns a consent code into a refresh token.

    **Read off `app.state` rather than called directly**, following `_rooms` and
    `_calendar`. The same reason applies with more force here: this is the one
    outbound call in the codebase whose *request* is the thing that has to be
    right — `access_type`, `prompt` and a redirect that matches the consent
    byte-for-byte — and a seam is what lets a test assert what was asked rather
    than what came back.
    """
    return getattr(request.app.state, "calendar_exchange", None) or exchange_code


async def calendar_consent_url(request: Request, user: CurrentUserDep) -> str:
    """Where to send this mentor to grant free/busy access.

    **The `state` carries the mentor and is sealed**, which is the whole CSRF
    control here: without it, an attacker could complete their *own* Google
    consent against a victim's session and attach their calendar to somebody
    else's account. The seal makes the mentor's id unforgeable and its ten-minute
    TTL makes a captured URL useless by the time anybody finds it.
    """
    client_id, _, redirect_uri, key = _calendar_oauth(request)
    state = sealed_value({"user_id": str(user["id"])}, key=key)
    return consent_url(client_id=client_id, redirect_uri=redirect_uri, state=state)


async def calendar_connected(
    request: Request, session: SessionDep, code: str = "", state: str = ""
) -> UUID:
    """Complete the grant Google is redirecting back from.

    **No `CurrentUserDep`, and that is the point of the sealed state.** Google
    redirects a browser here; there is no bearer token on that request, so the
    mentor's identity has to travel in the `state` we issued — which is exactly
    why it is sealed rather than merely passed.

    Raises :class:`AuthenticationError` for a state we did not issue, so a
    forged callback answers the same way an unauthenticated request does.
    """
    client_id, client_secret, redirect_uri, key = _calendar_oauth(request)
    if not code or not state:
        raise ValidationError("this is not a completed consent")

    try:
        opened = unsealed_value(state, key=key)
    except SealError as exc:
        raise AuthenticationError(str(exc)) from exc
    user_id = UUID(str(opened["user_id"]))

    tokens = _token_exchange(request)(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    # **Checked here as well as in the adapter**, which is not belt-and-braces:
    # the adapter is swappable through `app.state`, and the failure this guards
    # against is a `KeyError` reaching the client as a 500 rather than as the
    # refusal it is. Indexing a dict that came from outside is the mistake.
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise VenueUnavailableError("google returned no refresh token")
    # **Here as well as in the adapter, for the reason given just above.** The
    # exchange is swappable through `app.state`, so a refusal that lives only
    # inside the real adapter is missing from every wiring that replaces it —
    # which an integration test proved by watching the API answer `200` to a
    # consent that granted no calendar access.
    require_calendar_scope(tokens)

    await connect(
        session,
        user_id,
        # **After the refresh-token check, and never before it.** The grant is
        # the thing the mentor came to make; naming the account is a label on
        # it. `account_email` answers `None` rather than raising for exactly
        # that reason, so a userinfo failure costs the label and not the
        # connection.
        account_email=_named_account(request, tokens),
        # **Nothing here names the Google account.** It would come from an
        # `id_token`, and Google issues one only when `openid` is among the
        # scopes — ADR 0012 asks for `calendar.freebusy` alone, so
        # `external_account_id` stays null rather than the consent screen
        # growing a second line to fill it.
        refresh_token_encrypted=seal(refresh_token, key=key),
    )
    await session.commit()
    return user_id


async def own_calendar(user: CurrentUserDep, session: SessionDep) -> dict[str, Any] | None:
    """This mentor's live grant, or ``None`` if they have not connected."""
    return await active_connection(session, user["id"])


async def disconnected_calendar(user: CurrentUserDep, session: SessionDep) -> bool:
    """Revoke this mentor's grant. ``False`` if they had none.

    The route turns that into a `404` rather than an idempotent `204`: a mentor
    who thinks they disconnected something needs to know they did not, and the
    thing they would be wrong about is whether a credential still exists.
    """
    removed = await disconnect(session, user["id"])
    await session.commit()
    return removed


CalendarConsentDep = Annotated[str, Depends(calendar_consent_url)]
CalendarConnectedDep = Annotated[UUID, Depends(calendar_connected)]
OwnCalendarDep = Annotated[dict[str, Any] | None, Depends(own_calendar)]
DisconnectedCalendarDep = Annotated[bool, Depends(disconnected_calendar)]


async def own_conferencing(user: CurrentUserDep, session: SessionDep) -> dict[str, Any] | None:
    """The caller's saved default video provider, or ``None`` if never chosen.
    `404` for a caller with no live mentor profile."""
    is_mentor, saved = await own_default_option(session, user["id"])
    if not is_mentor:
        raise NotFoundError("this user has no mentor profile")
    return saved


async def updated_conferencing(
    payload: ConferencingWrite, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Make the caller's choice their default and return it as saved."""
    if not await set_default_option(session, user["id"], payload.provider, payload.custom_url):
        raise NotFoundError("this user has no mentor profile")
    await session.commit()
    return {"provider": payload.provider, "custom_url": payload.custom_url}


OwnConferencingDep = Annotated[dict[str, Any] | None, Depends(own_conferencing)]
UpdatedConferencingDep = Annotated[dict[str, Any], Depends(updated_conferencing)]
