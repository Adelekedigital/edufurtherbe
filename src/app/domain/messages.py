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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from app.core.errors import AppError

__all__ = [
    "ALIASES",
    "RESOLVERS",
    "MessageContext",
    "UnresolvedVariableError",
    "build_variables",
]


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

    starts_at: dt.datetime | None = None
    topic: str | None = None
    detail: str | None = None
    venue: str | None = None
    respond_by: dt.datetime | None = None

    session_id: str | None = None
    app_base_url: str = ""

    #: Values carried on the outbox row rather than derived from the session —
    #: what a person actually wrote, and which of them acted. They are facts
    #: about the *event*, unknowable from the session row afterwards.
    extras: Mapping[str, str] = field(default_factory=dict)


#: Where the session page and the dashboard live in the front-end.
#:
#: **Constants rather than inline strings**, because the shapes are not settled:
#: when they are, this is the one place that changes. They are joined to
#: `APP_BASE_URL`, which is the *application's* origin and deliberately not
#: `PUBLIC_BASE_URL` — that one is this service's own, and a link built from it
#: would send a mentor to the API.
SESSION_PATH = "/sessions/{session_id}"
DASHBOARD_PATH = "/dashboard"


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


def _session_url(context: MessageContext) -> str:
    """The EduFurther **session page** — never the meeting link.

    Meeting links are deliberately withheld until the join window opens, five
    minutes before the start, so that nobody joins early and so a Join press is
    something the platform can record. Putting `meeting_url` here would hand the
    room out days in advance and undo both.
    """
    session_id = _needs(context.session_id, "sessionUrl")
    return f"{context.app_base_url.rstrip('/')}{SESSION_PATH.format(session_id=session_id)}"


def _hours_left(context: MessageContext) -> str:
    """Whole hours until the mentor's deadline, floored, never below zero.

    Floored because "3 hours left" reading 3 when 3.9 remain is the safe
    direction — a mentor told less time than they have answers sooner.
    """
    respond_by = context.respond_by
    starts_at = context.starts_at
    if respond_by is None or starts_at is None:
        raise UnresolvedVariableError("this message has no response deadline")
    remaining = respond_by - dt.datetime.now(dt.UTC)
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
        "%A %d %B %Y"
    ),
    "sessionTime": lambda c: _local(_as_moment(c, "sessionTime"), c.recipient_timezone).strftime(
        "%H:%M"
    ),
    "sessionTimezone": lambda c: _needs(c.recipient_timezone, "sessionTimezone"),
    "sessionTopic": lambda c: _needs(c.topic, "sessionTopic"),
    "sessionDetail": lambda c: _needs(c.detail, "sessionDetail"),
    "location": lambda c: _needs(c.venue, "location"),
    "sessionUrl": _session_url,
    "dashboardUrl": lambda c: f"{c.app_base_url.rstrip('/')}{DASHBOARD_PATH}",
    "hours": _hours_left,
    "reasonTitle": lambda c: _needs(c.extras.get("reason_title"), "reasonTitle"),
    "reasonMessage": lambda c: _needs(c.extras.get("reason_text"), "reasonMessage"),
    "cancelInitiator": lambda c: _needs(c.extras.get("cancel_initiator"), "cancelInitiator"),
    "intervalTime": lambda c: _needs(c.extras.get("interval"), "intervalTime"),
}


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
}


def _other_party(context: MessageContext) -> str:
    """Whoever the recipient is not. The one recipient-relative resolver."""
    if context.recipient_name == context.mentor_name:
        return _needs(context.mentee_name, "attendee")
    return _needs(context.mentor_name, "attendee")


RESOLVERS["attendee"] = _other_party


def build_variables(declared: Iterable[str], context: MessageContext) -> dict[str, str]:
    """The values this template asked for, or a refusal naming the first missing.

    ``declared`` comes from the provider — the template's own list of merge
    fields — so this never guesses what a message contains.

    **An empty ``declared`` is a caller error, not an empty result.** Loops
    reports no variables for an *unpublished* template, and treating that as
    "this template needs nothing" sends a blank email — the failure this module
    exists to prevent, wearing a convincing disguise. The caller checks for it;
    the check is repeated here because both call sites would otherwise have to
    remember.
    """
    names = list(declared)
    if not names:
        raise UnresolvedVariableError(
            "the template declares no variables, which means it is unpublished "
            "rather than that it needs none"
        )

    built: dict[str, str] = {}
    for name in names:
        resolver = RESOLVERS.get(ALIASES.get(name, name))
        if resolver is None:
            raise UnresolvedVariableError(f"nothing knows how to produce {name!r}")
        built[name] = resolver(context)
    return built
