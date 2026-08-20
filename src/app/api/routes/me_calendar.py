"""A mentor connecting the calendar the platform reads their busy hours from.

**One grant, and it is the narrowest Google offers.** ADR 0012 asks a mentor for
``calendar.freebusy`` and nothing else, so the consent screen says exactly one
thing: *"View your availability in your calendars."* The calendar the platform
*writes* session events to is EduFurther's own account and needs no consent from
anybody — conflating the two is what once made a docstring claim the event
writer needed this table.

**Reading a mentor's busy hours is not built yet.** That is the free/busy
subtraction in `slot_store`, and it lands next, against these rows.

The split is deliberate rather than incidental: `slot_store` holds the
most-tested query in the codebase, and reviewing a change to it alongside a
consent flow that stores a credential means reviewing neither carefully. What
ships here is whole on its own terms — a mentor can connect, see what is
connected, and disconnect — so it is a first half rather than a fragment.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import (
    CalendarConnectedDep,
    CalendarConsentDep,
    DisconnectedCalendarDep,
    OwnCalendarDep,
)
from app.api.schemas.availability import CalendarConnectionRead
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/me/calendar", tags=["availability"])

OWNER_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}

#: Only `/connect` reads the OAuth settings. Reading your connection and
#: disconnecting it touch nothing but this service's own database, so listing
#: this on them would document a response they cannot produce.
UNCONFIGURED: dict[int | str, dict[str, str]] = {
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "description": (
            "Calendar connection is not configured on this deployment. An "
            "operator fault rather than a caller fault, so the detail is "
            "withheld — it would name settings."
        )
    },
}


@router.get(
    "",
    response_model=CalendarConnectionRead | None,
    summary="Whether your calendar is connected",
    description=(
        "`null` when you have never connected, or have disconnected. Otherwise "
        "when the grant was made, when it was last used, and what went wrong if "
        "anything did.\n\n"
        "**No token is returned, ever.** The credential is stored encrypted and "
        "is read only at the moment it is used."
    ),
    responses=OWNER_RESPONSES,
)
async def read_own_calendar(connection: OwnCalendarDep) -> CalendarConnectionRead | None:
    return CalendarConnectionRead.from_row(connection) if connection else None


@router.get(
    "/connect",
    summary="Start connecting your calendar",
    description=(
        "Returns the Google URL to send the mentor to. **A URL rather than a "
        "redirect**, because the caller is a single-page application: a `302` "
        "from an XHR is followed by the browser and lands the consent screen "
        "inside a fetch, where nobody can see it.\n\n"
        "The link carries a sealed `state` that names you and expires in ten "
        "minutes. That is the CSRF control: without it somebody could complete "
        "their own Google consent against your session and attach **their** "
        "calendar to your account.\n\n"
        "Connecting again replaces the existing grant, which is how a mentor "
        "switches Google accounts."
    ),
    responses=OWNER_RESPONSES | UNCONFIGURED,
)
async def start_connecting(url: CalendarConsentDep) -> dict[str, str]:
    return {"consent_url": url}


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect your calendar",
    description=(
        "Stops the platform reading your busy hours, and **destroys the stored "
        "credential** — a disconnect that left us able to read the calendar it "
        "just stopped reading would not be one.\n\n"
        "The row survives, marked revoked, so *that you once connected* stays "
        "answerable. Connecting again is a fresh grant rather than an undo, "
        "because Google has to be asked again either way.\n\n"
        "`404` if there was nothing connected."
    ),
    responses=OWNER_RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "Nothing was connected. Stated rather than treated as an "
                "idempotent success — a mentor who believes they revoked a "
                "credential needs to know they did not."
            )
        }
    },
)
async def stop_connecting(removed: DisconnectedCalendarDep) -> None:
    if not removed:
        raise NotFoundError("no calendar is connected")


#: **A second router, not a second module.** Google redirects a *browser*
#: here, so it carries no bearer token and belongs nowhere near `/me` — but
#: giving it its own file would collide with the callbacks module the reminder
#: work creates in a parallel branch, and two branches adding one filename is a
#: merge conflict neither gains from. It keeps its own prefix and its own tag,
#: which is what actually separates them.
callback_router = APIRouter(prefix="/api/v1/callbacks", tags=["callbacks"])


@callback_router.get(
    "/google/calendar",
    summary="Complete a mentor's calendar consent",
    description=(
        "**Google redirects a browser here**, so there is no bearer token on "
        "the request — the mentor's identity travels in the sealed `state` "
        "issued when the consent started, which is exactly why it is sealed "
        "rather than merely passed.\n\n"
        "A `state` we did not issue, or one older than ten minutes, is `401`. "
        "That is the CSRF control: without it somebody could complete their own "
        "Google consent against a victim's session and attach **their** "
        "calendar to somebody else's account.\n\n"
        "**Refused if Google returns no refresh token**, which it does when the "
        "grant was not fresh. Storing the access token alone would give a "
        "connection that works for an hour and then stops with nothing saying "
        "why."
    ),
    responses=UNCONFIGURED
    | {
        status.HTTP_401_UNAUTHORIZED: {
            "description": "The `state` is absent, expired, tampered with, or not ours."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Google did not send a completed consent."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": (
                "Google refused the code, or returned no refresh token. Nothing "
                "is stored — start the consent again."
            )
        },
    },
)
async def complete_calendar_consent(user_id: CalendarConnectedDep) -> dict[str, str]:
    """Returns the mentor's id rather than redirecting.

    **A redirect would need a front-end URL this service does not know.**
    `public_base_url` is the API's own, and guessing an application route is how
    a deployment ends up redirecting somewhere that does not exist. A client
    opens this in a popup and closes it on the response, which is the ordinary
    shape for a consent flow in a single-page application.
    """
    return {"connected": str(user_id)}
