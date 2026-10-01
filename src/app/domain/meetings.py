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
from urllib.parse import urlsplit

from app.domain.enums import ConferencingProvider

__all__ = [
    "MAX_MEETING_LINK_LENGTH",
    "PLATFORM_DEFAULT_PROVIDER",
    "MeetingPlan",
    "meeting_link",
    "plan_for",
]

#: Where a mentor who never chose is held: EduFurther video (owner, 2026-10-01).
#: It needs only the platform's Daily key, where Meet needs the platform's Google
#: calendar. The last step of venue resolution and the default `/me/conferencing`
#: reports, so the two cannot disagree.
PLATFORM_DEFAULT_PROVIDER = ConferencingProvider.DAILY

#: A personal room link's longest allowed form.
MAX_MEETING_LINK_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class MeetingPlan:
    """What has to happen for this session to have somewhere to meet."""

    provider: ConferencingProvider

    #: Whether a room must be created before the calendar event. True only for
    #: Daily, whose rooms are their own API objects.
    needs_room: bool

    #: Whether the calendar event asks Google for a conference. True only for
    #: Meet — for anything else the event carries an existing URL as ordinary
    #: content, and asking would add a second link nobody chose.
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
    if parts.username is not None or parts.password is not None:
        raise ValueError("a link must not carry a username or password")
    return link
