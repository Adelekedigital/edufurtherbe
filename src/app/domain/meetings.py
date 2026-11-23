"""Where a session is held, and what has to be called to make that true.

**One rule with three answers, and the interesting one is that they are not
symmetric.** A Google Meet link is a *property of the calendar event* — you ask
for it on `events.insert` with `conferenceDataVersion=1` and Google hands it
back. A Daily room is its own object, created before any calendar sees it. A
custom venue is a URL the mentor already typed and nothing creates at all.

So "which provider" decides two independent things — whether a room is created,
and whether the calendar event carries a conference request — and getting the
second wrong is the failure with no error message: **request a Meet conference
on a session held in Daily and the event ends up with two links, with the
invitee clicking whichever the client renders first.**

``conferenceDataVersion=1`` is not expressed here because it is a transport
detail, but it is the same class of trap and it is measured: without it the API
accepts the write and silently drops the conference, which
`docs/calendar-spike-guide.md` records as indistinguishable from a permissions
refusal if you only read the response.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.emails import calendar_invite_address, is_gmail
from app.domain.enums import ConferencingProvider
from app.domain.messages import session_page_url

__all__ = [
    "MAX_MEETING_LINK_LENGTH",
    "OFFERING_VENUES",
    "PLATFORM_DEFAULT_PROVIDER",
    "MeetingPlan",
    "call_exit_url",
    "meet_invite_for",
    "meet_link_for",
    "meeting_link",
    "plan_for",
]

#: Where a mentor who never chose is held: EduFurther video (owner, 2026-10-01).
#: It needs only the platform's Daily key, where Meet needs the platform's Google
#: calendar. The last step of venue resolution and the default `/me/conferencing`
#: reports, so the two cannot disagree.
PLATFORM_DEFAULT_PROVIDER = ConferencingProvider.DAILY

#: What one offering may be pinned to (decision 289). Not `custom`: it needs a
#: URL, which only the mentor's default takes (`/me/conferencing`).
OFFERING_VENUES = frozenset({ConferencingProvider.DAILY, ConferencingProvider.GOOGLE_MEET})

#: A personal room link's longest allowed form.
MAX_MEETING_LINK_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class MeetingPlan:
    """What has to happen for this session to have somewhere to meet."""

    provider: ConferencingProvider

    #: Whether a room must be created before the calendar event. True only for
    #: Daily, whose rooms are their own API objects.
    needs_room: bool

    #: Whether Google makes the conference. True only for Meet, and **never at
    #: booking** (#384): the event is created without one and the Meet patched
    #: on at the last reminder or the first press of Join. On any other venue
    #: it would be a second link nobody chose.
    wants_conference: bool

    #: Whether the URL already exists and nothing needs to mint one. True only
    #: for a custom venue, where the mentor supplied it.
    #:
    #: **This is also the shape the model warns about.** A custom venue is one
    #: static room for every session, so back-to-back sessions share it and an
    #: early joiner walks into the previous one — `Session.meeting_url` calls
    #: that a privacy incident rather than a UX annoyance. The plan records the
    #: fact; whether the venue should be selectable at all is settled decision
    #: #124's question.
    reuses_a_static_room: bool


def plan_for(provider: ConferencingProvider) -> MeetingPlan:
    """The three answers, in one place so no caller infers them from the name.

    Written as an explicit table rather than as booleans derived from the
    provider at each call site: the two flags are independent, and a reader who
    assumes *needs a room* and *wants a conference* are opposites gets Meet
    right and Daily wrong.
    """
    return MeetingPlan(
        provider=provider,
        needs_room=provider is ConferencingProvider.DAILY,
        wants_conference=provider is ConferencingProvider.GOOGLE_MEET,
        reuses_a_static_room=provider is ConferencingProvider.CUSTOM,
    )


def meeting_link(url: str) -> str:
    """A personal room link, as a mentor may set it: `https`, with a host, and no
    credentials in it. Raises `ValueError` otherwise.

    The link is a bearer credential to the room, so it is never printed back in
    an error; a `user:password@` part would leak a second secret to every mentee.
    """
    link = url.strip()
    if len(link) > MAX_MEETING_LINK_LENGTH:
        raise ValueError(f"a link is at most {MAX_MEETING_LINK_LENGTH} characters")
    parts = urlsplit(link)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("a link must be an https:// address with a host")
    try:
        parts.port  # noqa: B018 - parsing the port is the check: it raises if malformed
    except ValueError:
        raise ValueError("a link's port must be a number from 0 to 65535") from None
    if parts.username is not None or parts.password is not None:
        raise ValueError("a link must not carry a username or password")
    return link


def meet_invite_for(provider: object, address: str | None) -> str | None:
    """The address a party was invited to a session's Meet as, or ``None`` when
    the session is not on Meet or the party has no address (decision 357).

    One answer for `SessionRead.meet_invite_email` and the link `/join` and
    `/door` hand over, so what a party is told and where they are sent cannot
    disagree. It is the invite's own rule over the party's stored address;
    nobody can change their address after sign-up, so it is the address the
    invite went to (store the invited address if that ever changes).
    """
    if provider != ConferencingProvider.GOOGLE_MEET or not address:
        return None
    return calendar_invite_address(address)


def meet_link_for(url: str, invited: str | None) -> str:
    """A Meet link as one party is handed it: opening under their invited
    Google account (decision 357).

    ``authuser`` names the account Google opens Meet as, or asks the person to
    sign in to. Without it a rejoin in a browser that is already on Meet opens
    under that browser's default account, which Meet treats as uninvited, so
    the party waits to be admitted. **Only a Gmail address is named**: it is a
    Google account by construction, where another address may not be one, and
    naming it would send that person to a sign-in for an account that does not
    exist. Any query the link has is kept, blanks included, and an
    ``authuser`` already on it is replaced.
    """
    if not invited or not is_gmail(invited):
        return url
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "authuser"
    ]
    query.append(("authuser", invited))
    return urlunsplit(parts._replace(query=urlencode(query)))


#: Where a mentor lands after leaving a call: their dashboard. A mentee lands on
#: the session page with `?from=call`, which opens the review prompt when the
#: session can be reviewed (owner, 2026-11-22; frontend reads the marker).
MENTOR_CALL_EXIT_PATH = "/dashboard"
MENTEE_CALL_EXIT_QUERY = "from=call"


def call_exit_url(app_base_url: str, session_id: str, *, is_mentor: bool) -> str | None:
    """Where Daily sends one participant when they leave the call, or ``None``
    with no application origin configured (then Daily shows its own page).

    Built from the configured origin, never the request, so staging and
    production each send people to their own app."""
    if not app_base_url:
        return None
    if is_mentor:
        return f"{app_base_url.rstrip('/')}{MENTOR_CALL_EXIT_PATH}"
    page = session_page_url(app_base_url, session_id)
    return f"{page}?{MENTEE_CALL_EXIT_QUERY}" if page else None
