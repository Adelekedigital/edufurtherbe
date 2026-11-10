"""What a message says, as the values its template asked for.

**The template declares; this resolves.** Loops publishes each template's merge
fields, so the list of what a message needs is not duplicated here. What lives
here is one resolver per variable *name* — nine templates share roughly twenty
distinct names, because `sessionDate` appears in nearly every one, so a registry
keyed by name is a quarter of the work of a field list per template and shares by
construction.

**Pure, and in `domain` for the usual reason.** Which words a mentee reads is a
product fact. Nothing here touches a database or a provider: the caller loads the
context and this turns it into strings.

**Every name resolves or the send fails naming it.** That is the whole point.
Loops accepts a missing key and renders it as nothing, so an unresolved name is
otherwise invisible — a delivered email reading *"Hi , your session on "*. There
is no partial success worth having here: a blank field is worse than a message
that visibly did not go.

**Rendered per recipient, not per message.** A mentor in Lagos and a mentee in
Toronto are told the same instant in two different local times, and the outbox
already stores one row per recipient, which is what makes that possible.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.core.errors import AppError
from app.domain.email_lines import (
    LINE_NAMES,
    LINE_VARIABLES,
    LINES,
    OWNED_BY,
    PLACEHOLDER,
    SHARED_SWITCHES,
    case_for,
    group_of,
    marks_shared,
    variant_of,
)
from app.domain.email_values import (
    FALLBACKS,
    NO_REASON_TITLE,
    SESSION_TOPIC_FALLBACK,
    VENUE_FALLBACK,
)
from app.domain.enums import EmailLine, SessionReasonCode
from app.domain.message_emails import FEW_MAX, SENDER_ROWS
from app.domain.notifications import Notification
from app.domain.sessions import request_deadline

__all__ = [
    "ALIASES",
    "DATE_FORMAT",
    "DELETED_PARTY_LABELS",
    "LINE_VARIABLES",
    "NO_REASON_TITLE",
    "REASON_TITLES",
    "RESOLVERS",
    "SESSION_TOPIC_FALLBACK",
    "VENUE_FALLBACK",
    "LineNote",
    "MessageContext",
    "UnresolvedVariableError",
    "build_variables",
    "resolve",
    "session_page_url",
]


#: What a message says in place of someone who has deleted their account (#288).
#:
#: Owner, 2026-09-30: a queued message about them is **still sent** — the
#: recipient still needs the reminder — but names nobody who left. Keyed by the
#: role the deleted person had in the session, so the recipient reads "your
#: mentor" or "your mentee". The one place these words live.
DELETED_PARTY_LABELS = {"mentor": "your mentor", "mentee": "your mentee"}

#: A reason code in words, for `reasonTitle`. The one place they live.
REASON_TITLES: dict[SessionReasonCode, str] = {
    SessionReasonCode.MENTOR_UNAVAILABLE: "The mentor is unavailable",
    SessionReasonCode.MENTEE_NO_LONGER_NEEDED: "No longer needed",
    SessionReasonCode.SCHEDULING_CONFLICT: "Scheduling conflict",
    SessionReasonCode.TECHNICAL_ISSUE: "Technical issue",
    SessionReasonCode.MENTOR_NO_SHOW: "The mentor did not attend",
    SessionReasonCode.MENTEE_NO_SHOW: "The mentee did not attend",
    SessionReasonCode.EXPIRED_NO_RESPONSE: "No response in time",
    SessionReasonCode.RESCHEDULED: "Rescheduled",
    SessionReasonCode.ADMIN_ACTION: "Changed by EduFurther",
    SessionReasonCode.OTHER: "Other",
    SessionReasonCode.ACCOUNT_DELETED: "Account deleted",
}


@dataclass(frozen=True, slots=True)
class LineNote:
    """One thing a line worked around at send time (PRD 28, *Sending*).

    ``fallback``: ``value`` was empty, so its catalogue fallback was said.
    ``override_failed``: the admin's text could not fill, so the default went.
    """

    kind: str
    email: str
    variant: str
    line: str
    value: str | None = None


class UnresolvedVariableError(AppError):
    """A template asked for something nothing here can produce.

    Raised rather than substituting an empty string, because an empty string is
    exactly the failure this module exists to prevent and it arrives looking
    like success.
    """


@dataclass(frozen=True, slots=True)
class MessageContext:
    """Everything a resolver may read. Loaded by the caller, never fetched here.

    **The recipient is a first-class field**, not one of the two parties picked
    out later. A copy of a message belongs to one person: the date is rendered
    in *their* zone and the greeting is *their* name, and making that implicit
    is how a message ends up addressed to the wrong party.
    """

    recipient_name: str
    recipient_timezone: str
    mentor_name: str
    mentee_name: str
    recipient_first_name: str | None = None

    starts_at: dt.datetime | None = None
    topic: str | None = None
    #: The offering's name, the subject when nobody wrote a topic.
    session_type_name: str | None = None
    detail: str | None = None
    venue: str | None = None
    respond_by: dt.datetime | None = None
    duration_minutes: int | None = None

    session_id: str | None = None
    app_base_url: str = ""

    #: Which email this is (`emailType`), and the recipient's side of the
    #: session or profile it is about, ``"mentor"`` / ``"mentee"``, or ``""``
    #: when they are neither (`recipientRole`). Decision 317.
    notification: Notification | None = None
    recipient_role: str = ""

    #: Values carried on the outbox row rather than derived from the session —
    #: what a person actually wrote, and which of them acted. They are facts
    #: about the *event*, unknowable from the session row afterwards.
    extras: Mapping[str, str] = field(default_factory=dict)

    #: An admin's wording for this email, ``(variant, line) -> text`` (decision
    #: 318), laid over its default lines. Empty means every line is its default.
    line_overrides: Mapping[tuple[str, str], str] = field(default_factory=dict)


#: Where the session page and the dashboard live in the front-end.
#:
#: **Constants rather than inline strings**, because the shapes are not settled:
#: when they are, this is the one place that changes. They are joined to
#: `APP_BASE_URL`, which is the *application's* origin and deliberately not
#: `PUBLIC_BASE_URL` — that one is this service's own, and a link built from it
#: would send a mentor to the API.
#: How every date in an email reads: "Fri 20 November 2026" (owner,
#: 2026-11-10: a three-letter day keeps the details card's one-line WHEN short).
DATE_FORMAT = "%a %d %B %Y"

SESSION_PATH = "/sessions/{session_id}"
DASHBOARD_PATH = "/dashboard"
CALENDAR_PATH = "/calendar"
EXPLORE_PATH = "/explore"
#: Where an applicant follows their mentor application (Mentor Onboarding B3).
MENTOR_APPLICATION_PATH = "/mentor-application"
#: One pending request, open on the Bookings page's pending tab, where the
#: mentor answers it (decision 294).
PENDING_REQUEST_PATH = "/bookings?tab=pending&booking={session_id}"
#: Messages (decision 303): the inbox, one chat by the other person's id, and
#: the settings modal (Messages PRD, "Links").
MESSAGES_PATH = "/messages"
CHAT_PATH = "/messages?with={user_id}"
MESSAGE_SETTINGS_PATH = "/messages?settings=1"


def _local(moment: dt.datetime, timezone: str) -> dt.datetime:
    return moment.astimezone(ZoneInfo(timezone))


def _needs(value: object, name: str) -> str:
    """A resolver's answer, or a refusal naming what was missing.

    A template asking for `hours` on a cancellation is a template pointed at the
    wrong message. Returning empty would send it anyway.
    """
    if value is None or value == "":
        raise UnresolvedVariableError(f"this message has no value for {name!r}")
    return str(value)


def _app_link(context: MessageContext, path: str, name: str) -> str:
    """An absolute link into the application, or a refusal.

    Every link goes through here: with no application origin configured a
    link would go out as a bare path that no mail client can open.
    """
    return _absolute(_needs(context.app_base_url, name), path)


def _absolute(app_base_url: str, path: str) -> str:
    """The origin and a path joined: the one place a link into the app is built."""
    return f"{app_base_url.rstrip('/')}{path}"


def session_page_url(app_base_url: str, session_id: str) -> str | None:
    """The absolute link to a session's page, or ``None`` with no application
    origin configured.

    **The one link to a session the platform hands out**, in emails and on
    calendar invites alike (#389), so the two cannot point at different pages.
    It is where Join and Rejoin are pressed, and the page redirects a session it
    does not draw, so the link stays safe on an invite that outlives a
    cancellation. The id is encoded as a single path segment.
    """
    if not app_base_url:
        return None
    return _absolute(app_base_url, SESSION_PATH.format(session_id=quote(session_id, safe="")))


def _session_url(context: MessageContext) -> str:
    """The EduFurther **session page** — never the meeting link.

    Meeting links are deliberately withheld until the join window opens
    (`join_opens_at`), so that nobody joins early and so a Join press is
    something the platform can record. Putting `meeting_url` here would hand the
    room out days in advance and undo both.
    """
    session_id = _needs(context.session_id, "sessionUrl")
    return _needs(session_page_url(context.app_base_url, session_id), "sessionUrl")


def _pending_request_url(context: MessageContext) -> str:
    """The request on the Bookings page's pending tab, where the mentor answers it."""
    session_id = quote(_needs(context.session_id, "pendingRequestUrl"), safe="")
    return _app_link(
        context, PENDING_REQUEST_PATH.format(session_id=session_id), "pendingRequestUrl"
    )


def _topic(context: MessageContext) -> str:
    """What the session is about: the topic written, else the offering's name."""
    return context.topic or context.session_type_name or SESSION_TOPIC_FALLBACK


def _reason_title(context: MessageContext) -> str:
    """The code in words; "No reason given" only when nothing at all was given.

    A written reason with no code gets no title, so the email does not read
    "No reason given" directly above the reason.
    """
    code = context.extras.get("reason_code")
    if code:
        return REASON_TITLES[SessionReasonCode(code)]
    return "" if context.extras.get("reason_text") else NO_REASON_TITLE


def _initiator(context: MessageContext) -> str:
    """Who called it off, named at send time so a deleted party is not named."""
    side = _needs(context.extras.get("cancel_initiator"), "cancelInitiator")
    return context.mentor_name if side == "mentor" else context.mentee_name


def _hours_left(context: MessageContext) -> str:
    """Whole hours until the mentor's deadline, floored, never below zero.

    Floored because "3 hours left" reading 3 when 3.9 remain is the safe
    direction — a mentor told less time than they have answers sooner.
    """
    if context.starts_at is None:
        raise UnresolvedVariableError("this message has no response deadline")
    # A request with no `respond_by` lapses at its start (`request_deadline`).
    remaining = request_deadline(context.respond_by, context.starts_at) - dt.datetime.now(dt.UTC)
    return str(max(0, int(remaining.total_seconds() // 3600)))


#: Name to value. **One entry per distinct name**, shared by every template that
#: asks for it.
RESOLVERS: dict[str, Callable[[MessageContext], str]] = {
    "recipientName": lambda c: _needs(c.recipient_name, "recipientName"),
    "mentorName": lambda c: _needs(c.mentor_name, "mentorName"),
    "menteeName": lambda c: _needs(c.mentee_name, "menteeName"),
    # `%-d` is not portable to Windows, so the day is formatted without padding
    # by hand. The suite runs on both.
    "sessionDate": lambda c: _local(_as_moment(c, "sessionDate"), c.recipient_timezone).strftime(
        DATE_FORMAT
    ),
    "sessionTime": lambda c: _local(_as_moment(c, "sessionTime"), c.recipient_timezone).strftime(
        "%H:%M"
    ),
    "sessionTimezone": lambda c: _needs(c.recipient_timezone, "sessionTimezone"),
    "sessionTopic": _topic,
    "sessionDetail": lambda c: c.detail or "",
    "location": lambda c: c.venue or VENUE_FALLBACK,
    "sessionUrl": _session_url,
    "dashboardUrl": lambda c: _app_link(c, DASHBOARD_PATH, "dashboardUrl"),
    "exploreUrl": lambda c: _app_link(c, EXPLORE_PATH, "exploreUrl"),
    "recipientFirstName": lambda c: (
        c.recipient_first_name or _needs(c.recipient_name, "recipientFirstName")
    ),
    "creditCount": lambda c: _needs(c.extras.get("credit_count"), "creditCount"),
    "calendarUrl": lambda c: _app_link(c, CALENDAR_PATH, "calendarUrl"),
    "applicationUrl": lambda c: _app_link(c, MENTOR_APPLICATION_PATH, "applicationUrl"),
    # A decline's note, which is optional: empty when the reviewer left none.
    "declineReason": lambda c: c.extras.get("reason", ""),
    # The return reminder is one template for a dated pause and an undated one,
    # so the side that does not apply is deliberately empty, not refused.
    "returnOn": lambda c: (
        dt.date.fromisoformat(c.extras["return_on"]).strftime(DATE_FORMAT)
        if c.extras.get("return_on")
        else ""
    ),
    "daysUntilReturn": lambda c: c.extras.get("days_until_return", ""),
    "daysPaused": lambda c: c.extras.get("days_paused", ""),
    "hours": _hours_left,
    "reasonTitle": _reason_title,
    "reasonMessage": lambda c: c.extras.get("reason_text") or "",
    "cancelInitiator": _initiator,
    "intervalTime": lambda c: _needs(c.extras.get("interval"), "intervalTime"),
    # A suggested time (#339), in the recipient's zone like the session's own.
    "suggestedDate": lambda c: _extra_moment(c, "suggested_starts_at", "suggestedDate").strftime(
        DATE_FORMAT
    ),
    "suggestedTime": lambda c: _extra_moment(c, "suggested_starts_at", "suggestedTime").strftime(
        "%H:%M"
    ),
    "holdUntilTime": lambda c: _extra_moment(c, "held_until", "holdUntilTime").strftime("%H:%M"),
    "suggestedAfter": lambda c: _needs(c.extras.get("suggested_after"), "suggestedAfter"),
    # A mentee's reminder about a pending request (decision 294): their words
    # from the row, and a link to the request itself.
    "reminderMessage": lambda c: _needs(c.extras.get("reminder_message"), "reminderMessage"),
    "pendingRequestUrl": _pending_request_url,
    # The message emails (decision 303).
    "messagesUrl": lambda c: _app_link(c, MESSAGES_PATH, "messagesUrl"),
    "settingsUrl": lambda c: _app_link(c, MESSAGE_SETTINGS_PATH, "settingsUrl"),
    "sessionName": lambda c: c.session_type_name or SESSION_TOPIC_FALLBACK,
    "chatUrl": lambda c: _button_url(c, "chatUrl"),
    "sessionDuration": lambda c: f"{_needs(c.duration_minutes, 'sessionDuration')} min",
}


def _button_url(context: MessageContext, name: str) -> str:
    """One person's chat when the email is about one person, else Messages."""
    other = context.extras.get("reply_to")
    path = CHAT_PATH.format(user_id=quote(other, safe="")) if other else MESSAGES_PATH
    return _app_link(context, path, name)


#: A message email's values, carried on its row by `domain.message_emails`:
#: these always have one, so a template asking for one on another email fails.
_MESSAGE_EMAIL_VALUES = {
    "repliesName": "replies_name",
    "previewText": "preview_text",
    "senderFirstName": "sender_first_name",
    "unreadCount": "unread_count",
    "senderFirstNames": "sender_first_names",
}

#: A card or a row that may be empty: the second card of a one-message email,
#: a sender row past the senders, a message that is a file alone.
_MESSAGE_EMAIL_SLOTS = {
    **{
        f"message{n}{name}": f"message{n}_{key}"
        for n in range(1, FEW_MAX + 1)
        for name, key in (
            ("Name", "name"),
            ("Initials", "initials"),
            ("Text", "text"),
            ("FileName", "file_name"),
        )
    },
    **{
        f"sender{n}{name}": f"sender{n}_{key}"
        for n in range(1, SENDER_ROWS + 1)
        for name, key in (("Name", "name"), ("Initials", "initials"), ("Count", "count"))
    },
    "otherSenders": "other_senders",
}


def _message_time(context: MessageContext, key: str, name: str) -> str:
    """A message card's time in the recipient's zone, empty with no card."""
    if not context.extras.get(key):
        return ""
    return _extra_moment(context, key, name).strftime("%H:%M")


def _carried(key: str, name: str) -> Callable[[MessageContext], str]:
    """A value the message email's row carries, or a refusal naming it."""
    return lambda c: _needs(c.extras.get(key), name)


def _slot(key: str) -> Callable[[MessageContext], str]:
    """A card or row value, empty when the email has no such card or row."""
    return lambda c: c.extras.get(key, "")


def _card_time(n: int) -> Callable[[MessageContext], str]:
    return lambda c: _message_time(c, f"message{n}_sent_at", f"message{n}Time")


RESOLVERS |= {name: _carried(key, name) for name, key in _MESSAGE_EMAIL_VALUES.items()}
RESOLVERS |= {name: _slot(key) for name, key in _MESSAGE_EMAIL_SLOTS.items()}
RESOLVERS |= {f"message{n}Time": _card_time(n) for n in range(1, FEW_MAX + 1)}


def _extra_moment(context: MessageContext, key: str, name: str) -> dt.datetime:
    """An instant the event carried, in the recipient's zone."""
    return _local(
        dt.datetime.fromisoformat(_needs(context.extras.get(key), name)), context.recipient_timezone
    )


def _as_moment(context: MessageContext, name: str) -> dt.datetime:
    if context.starts_at is None:
        raise UnresolvedVariableError(
            f"this message is not about a session, so {name!r} has no value"
        )
    return context.starts_at


#: The names the current Loops templates use, onto the resolvers above.
#:
#: **This is what makes renaming Loops optional.** The nine templates are two
#: generations that disagree — `sessiondate` against `sessionDate`, and more
#: importantly recipient-relative `name`/`attendee` against role-absolute
#: `mentorName`/`menteeName`. Aliases let both work untouched, so the rename is
#: housekeeping rather than a nine-template pass blocking the first send.
#:
#: `attendee` is the one entry that is **not** a rename. It means "the other
#: person", which cannot be read without knowing who received the copy — new
#: templates should say `mentorName` or `menteeName`.
ALIASES: dict[str, str] = {
    "name": "recipientName",
    "sessiondate": "sessionDate",
    "sessiontime": "sessionTime",
    "topic": "sessionTopic",
    "topicDiscuss": "sessionDetail",
    "discuss": "sessionDetail",
    "sessionlink": "sessionUrl",
    "webUrl": "sessionUrl",
    "dashlink": "dashboardUrl",
    "cancelmessage": "reasonMessage",
    "cancelinitiator": "cancelInitiator",
    "intervaltime": "intervalTime",
    "sessTopic": "sessionTopic",
    "fName": "recipientFirstName",
    "bookLink": "exploreUrl",
    # A review is by the mentee and about the mentor, and its content lives on
    # the session page, so the link is that page rather than a quote.
    "reviewBy": "menteeName",
    "reviewFor": "mentorName",
    "reviewLink": "sessionUrl",
}


def _other_party(context: MessageContext) -> str:
    """Whoever the recipient is not. The one recipient-relative resolver."""
    if context.recipient_name == context.mentor_name:
        return _needs(context.mentee_name, "attendee")
    return _needs(context.mentor_name, "attendee")


RESOLVERS["attendee"] = _other_party


def _is_line(name: str) -> bool:
    """Whether ``name`` is one of the variables the backend's lines fill."""
    return ALIASES.get(name, name) in LINE_NAMES


#: Where the build in progress collects its `LineNote`s (`build_variables`'
#: ``notes``). Per call, so no context, and no copy of one, shares a list.
_NOTES: ContextVar[list[LineNote] | None] = ContextVar("email_line_notes", default=None)


def _noted(notes: Iterable[LineNote]) -> None:
    found = _NOTES.get()
    if found is not None:
        found.extend(notes)


def _copy_case(context: MessageContext, name: str) -> tuple[Notification, str]:
    """This copy's email and the `LINES` case it uses, for its lines and its
    button alike, or a refusal naming ``name``."""
    email = context.notification
    case = (
        None
        if email is None
        else case_for(email, context.extras.get("line_case", ""), context.recipient_role)
    )
    if email is None or case is None:
        raise UnresolvedVariableError(f"this email has no lines for {name!r}")
    return email, case


def _line(context: MessageContext, line: EmailLine) -> str:
    """One of this email's three lines, filled (PRD 28, *Sending*).

    The admin's override for its variant wins over the default. In these lines
    alone an empty value says its catalogue fallback, noted for the caller to
    log; a value with no fallback still refuses. An override that cannot fill
    at all (a value since removed) sends the default instead, noted as an
    error, rather than refusing the send.
    """
    email, case = _copy_case(context, LINE_VARIABLES[line])
    variant = variant_of(case)
    default = getattr(LINES[email][case], line)
    override = context.line_overrides.get((variant, line))
    note = functools.partial(LineNote, email=str(email), variant=variant, line=line)
    if override is not None:
        notes: list[LineNote] = []
        try:
            filled = _filled(override, context, notes, note)
        except UnresolvedVariableError:
            _noted([note(kind="override_failed")])
        else:
            _noted(notes)
            return filled
    notes = []
    try:
        return _filled(default, context, notes, note)
    finally:
        _noted(notes)


def _filled(
    text: str,
    context: MessageContext,
    notes: list[LineNote],
    note: Callable[..., LineNote],
) -> str:
    """A line with each ``{name}`` filled, an empty one by its fallback."""

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        canonical = ALIASES.get(name, name)
        if _is_line(name):
            raise UnresolvedVariableError(f"a line cannot name {name!r}")
        try:
            value = _resolve(name, context, shared=False)
        except UnresolvedVariableError:
            value = ""
        if value:
            return value
        fallback = FALLBACKS.get(canonical)
        if fallback is None:
            raise UnresolvedVariableError(f"this message has no value for {name!r}")
        notes.append(note(kind="fallback", value=canonical))
        return fallback

    return PLACEHOLDER.sub(one, text)


def _button_link(context: MessageContext) -> str:
    """Where the button goes: the email's own link, never an admin's."""
    email, case = _copy_case(context, "buttonUrl")
    return _resolve(LINES[email][case].link, context, shared=False)


RESOLVERS |= {
    "emailType": lambda c: _needs(c.notification, "emailType"),
    "recipientRole": lambda c: c.recipient_role,
    "buttonUrl": _button_link,
}
RESOLVERS |= {
    variable: functools.partial(_line, line=line) for line, variable in LINE_VARIABLES.items()
}


def _resolve(name: str, context: MessageContext, *, shared: bool) -> str:
    """One variable's value, or a refusal naming it.

    In a ``shared`` template a variable `OWNED_BY` some emails of a group is
    **empty for the others in that group**: their copy shows it in a hidden
    section. A template one email still uses alone, and every email outside
    the group, resolve or fail as they always have.
    """
    canonical = ALIASES.get(name, name)
    resolver = RESOLVERS.get(canonical)
    if resolver is None:
        raise UnresolvedVariableError(f"nothing knows how to produce {name!r}")
    owners = OWNED_BY.get(canonical)
    if (
        shared
        and owners is not None
        and context.notification is not None
        and context.notification not in owners
        and owners & group_of(context.notification)
    ):
        return ""
    return resolver(context)


def resolve(name: str, context: MessageContext) -> str:
    """One value as a line would say it: owned names are never left empty."""
    return _resolve(name, context, shared=False)


def build_variables(
    declared: Iterable[str],
    context: MessageContext,
    notes: list[LineNote] | None = None,
) -> dict[str, str]:
    """The values this template asked for, or a refusal naming the first missing.

    ``declared`` comes from the provider — the template's own list of merge
    fields — so this never guesses what a message contains.

    **An empty ``declared`` is a caller error, not an empty result.** Loops
    reports no variables for an *unpublished* template, and treating that as
    "this template needs nothing" sends a blank email — the failure this module
    exists to prevent, wearing a convincing disguise. The caller checks for it;
    the check is repeated here because both call sites would otherwise have to
    remember.

    ``notes`` collects what the lines worked around (`LineNote`), filled even
    when a later value refuses, so the caller can log a broken override.
    """
    names = list(declared)
    if not names:
        raise UnresolvedVariableError(
            "the template declares no variables, which means it is unpublished "
            "rather than that it needs none"
        )

    # **A shared template is one that shows the backend's lines** (or names
    # `emailType`). Only then is an `OWNED_BY` variable empty for the other
    # emails of its group, and only then are `SHARED_SWITCHES` sent.
    shared = any(marks_shared(ALIASES.get(name, name)) for name in names)
    if shared:
        names += [name for name in SHARED_SWITCHES if name not in names]
    # The lines first, whatever order the template declares: they are what
    # collects `notes`, so a refusal elsewhere never hides them.
    lines_first = sorted(names, key=lambda name: not _is_line(name))
    token = _NOTES.set(notes)
    try:
        built = {name: _resolve(name, context, shared=shared) for name in lines_first}
    finally:
        _NOTES.reset(token)
    return {name: built[name] for name in names}
