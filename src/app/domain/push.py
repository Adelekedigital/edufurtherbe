"""Web Push: a browser's subscription to this platform's notifications (#386).

The bounds live here once: the columns' `CHECK`s read them (and the request
models will), and a migration test pins the live constraints to them.

A subscription is what `PushSubscription.toJSON()` gives a browser (RFC 8030
for the endpoint, RFC 8291 for the keys): the push service's URL for one
browser profile, and the two keys a payload is encrypted to.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from app.core.config import BASE64URL_PATTERN
from app.domain.messages import CHAT_PATH, PENDING_REQUEST_PATH, SESSION_PATH
from app.domain.names import addressed_by
from app.domain.notifications import LAST_REMINDER_KIND, SESSION_REMINDER_KINDS, Notification

#: The longest push-service endpoint accepted, in bytes. Real ones run to a few
#: hundred; bytes, not characters, so it also keeps the unique index's entry
#: under Postgres's btree limit.
ENDPOINT_MAX_LENGTH = 2048

#: The longest `p256dh` key accepted, in characters: a 65-byte P-256 point is
#: 87 base64url characters (88 padded); the bound leaves room, not a rule.
P256DH_MAX_LENGTH = 128

#: The longest `auth` secret accepted, in characters: 16 bytes is 22 base64url
#: characters (24 padded).
AUTH_MAX_LENGTH = 64

#: Every endpoint is an `https://` URL of printable ASCII (`!` to `~`): a push
#: service is never reached in the clear, and real endpoints are
#: percent-encoded, so whitespace, control or multibyte characters mean junk.
ENDPOINT_PATTERN = "^https://[!-~]+$"

#: The base64url alphabet a browser's keys come in, optionally padded: the
#: same rule the server's own VAPID keys are read by.
KEY_PATTERN = BASE64URL_PATTERN

#: How many browsers one user may subscribe. Past it, the oldest (by its last
#: subscribe) is dropped: every push fans out to every row, so the bound keeps
#: one account from turning a send into a burst.
MAX_SUBSCRIPTIONS_PER_USER = 10

#: The push services browsers use, matched as the host itself or a subdomain
#: of it. **An allowlist, not a pattern**: the server POSTs to whatever is
#: stored, so a free-form `https://` URL would let any account aim it at an
#: internal address. Chrome, Opera, Samsung and Brave use FCM; Firefox Mozilla's
#: autopush; Safari Apple's; Edge Windows Push.
PUSH_SERVICE_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)


#: A plain DNS name: labels of letters, digits and hyphens, one dot apart. So
#: no parser disagreement (a backslash, an encoded slash, an empty label) can
#: make the host the sender reaches differ from the one checked here.
_HOST = r"[a-z0-9-]+(?:\.[a-z0-9-]+)*"


def is_push_service(endpoint: str) -> bool:
    """Whether `endpoint` is a URL this platform may push to.

    `https`, printable ASCII within `ENDPOINT_MAX_LENGTH` bytes, no userinfo,
    no explicit port, a host that is a plain DNS name, one of `PUSH_SERVICE_HOSTS` or a
    subdomain of one (on a dot boundary, so `evilfcm.googleapis.com.test` is
    not). Checked on subscribe and again before every send.
    """
    if not re.fullmatch(ENDPOINT_PATTERN, endpoint) or len(endpoint) > ENDPOINT_MAX_LENGTH:
        return False
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError:
        return False
    host = (url.hostname or "").rstrip(".")
    if not re.fullmatch(_HOST, host):
        return False
    if url.username is not None or url.password is not None or port is not None:
        return False
    return any(host == known or host.endswith("." + known) for known in PUSH_SERVICE_HOSTS)


#: How long after a message its push waits. A burst from one sender becomes
#: one push: each job sends only if its message is still the newest unread.
MESSAGE_PUSH_DELAY = dt.timedelta(seconds=5)

#: How long a push service holds a push for a browser that is offline. A day:
#: past that, the app's own badge and inbox say it better than a stale alert.
PUSH_TTL = dt.timedelta(days=1)


class PushType(StrEnum):
    """What a push is about; the service worker routes on it."""

    MESSAGE = "message"
    #: Somebody booked, or asked to book, the mentor.
    BOOKING_REQUEST = "booking_request"
    #: The mentor answered the mentee's request, or offered another time.
    BOOKING_RESPONSE = "booking_response"
    #: A confirmed session starts in minutes.
    SESSION_SOON = "session_soon"
    #: The other party moved, cancelled or withdrew it.
    SESSION_CHANGED = "session_changed"


#: How long a "starting soon" push may wait: the last reminder's own lead, as
#: it is queued that long before the start. Past it the session has started
#: and the push would be wrong, not late.
SESSION_SOON_TTL = SESSION_REMINDER_KINDS[LAST_REMINDER_KIND].before

#: `Urgency` (RFC 8030 section 5.3): what a push service may delay for battery.
NORMAL = "normal"
HIGH = "high"


@dataclass(frozen=True)
class PushMessage:
    """One push, as the service worker reads it (the agreed payload).

    **Never the content**: a lock screen shows `body`, so it names the event
    and who caused it, nothing they wrote.
    """

    type: PushType
    title: str
    body: str
    #: The in-app path the notification opens.
    url: str
    #: Collapses repeats on the device: a second push with the tag replaces it.
    tag: str
    #: The receiver's unread messages after the event, for the app icon.
    badge: int
    #: How the push is sent, not what it says: never in the payload.
    ttl: dt.timedelta = PUSH_TTL
    urgency: str = NORMAL

    def payload(self) -> bytes:
        return json.dumps(
            {
                "type": str(self.type),
                "title": self.title,
                "body": self.body,
                "url": self.url,
                "tag": self.tag,
                "badge": self.badge,
            },
            separators=(",", ":"),
        ).encode()


def message_push(
    sender_id: UUID, first_name: str | None, last_name: str | None, *, unread: int
) -> PushMessage:
    """The push for a new message: who sent it, never what it says."""
    return PushMessage(
        type=PushType.MESSAGE,
        title="New message",
        body=f"New message from {addressed_by(first_name, last_name)}",
        url=CHAT_PATH.format(user_id=sender_id),
        tag=f"message:{sender_id}",
        badge=unread,
    )


@dataclass(frozen=True)
class SessionPush:
    """What one session event pushes. `{name}` in `body` is the other party."""

    type: PushType
    title: str
    body: str
    #: `SESSION_PATH` or `PENDING_REQUEST_PATH`, formatted with the session id.
    path: str = SESSION_PATH
    ttl: dt.timedelta = PUSH_TTL
    urgency: str = NORMAL
    #: Who of the email's audience is pushed, when not all of them: the one
    #: who did not cause the event (ADR 0025 without the email's exception).
    only: str | None = None


#: Every session event that pushes (owner, 2026-11-07), keyed by the email it
#: rides with: the push is queued beside that email (`outbox.enqueue`).
SESSION_PUSHES: dict[Notification, SessionPush] = {
    Notification.SESSION_REQUESTED: SessionPush(
        PushType.BOOKING_REQUEST,
        "New booking request",
        "{name} wants to book a session with you",
        PENDING_REQUEST_PATH,
    ),
    # The email also goes to the mentee for its join link; they just booked it.
    Notification.SESSION_BOOKED: SessionPush(
        PushType.BOOKING_REQUEST, "New booking", "{name} booked a session with you", only="mentor"
    ),
    Notification.REQUEST_ACCEPTED: SessionPush(
        PushType.BOOKING_RESPONSE, "Booking accepted", "{name} accepted your booking"
    ),
    Notification.REQUEST_DECLINED: SessionPush(
        PushType.BOOKING_RESPONSE, "Booking declined", "{name} declined your booking"
    ),
    Notification.SESSION_TIME_SUGGESTED: SessionPush(
        PushType.BOOKING_RESPONSE, "New time suggested", "{name} suggested a new time"
    ),
    Notification.SESSION_LAST_REMINDER: SessionPush(
        PushType.SESSION_SOON,
        "Session starting soon",
        "Your session with {name} starts in 5 minutes",
        ttl=SESSION_SOON_TTL,
        urgency=HIGH,
    ),
    Notification.SESSION_CANCELLED: SessionPush(
        PushType.SESSION_CHANGED, "Session cancelled", "{name} cancelled your session"
    ),
    Notification.REQUEST_WITHDRAWN: SessionPush(
        PushType.SESSION_CHANGED, "Booking withdrawn", "{name} withdrew their booking request"
    ),
}

#: A move (decision 300) sends the booking's own email, so its push is chosen
#: here: the new session names the one it replaced. Only a mentee moves.
MOVED_PUSHES: dict[Notification, SessionPush] = {
    Notification.SESSION_REQUESTED: SessionPush(
        PushType.SESSION_CHANGED,
        "Reschedule request",
        "{name} asked to move your session",
        PENDING_REQUEST_PATH,
    ),
    Notification.SESSION_BOOKED: SessionPush(
        PushType.SESSION_CHANGED, "Session rescheduled", "{name} moved your session", only="mentor"
    ),
}


def session_push_spec(
    notification: Notification, *, recipient_role: str, moved: bool
) -> SessionPush | None:
    """What a session event pushes to a party in this role, or `None`: the one
    rule of who is pushed, read when the row is queued and when it is sent."""
    spec = (MOVED_PUSHES if moved else {}).get(notification) or SESSION_PUSHES.get(notification)
    if spec is None or (spec.only is not None and spec.only != recipient_role):
        return None
    return spec


def session_push(
    spec: SessionPush, *, session_id: UUID, other_name: str, unread: int, ttl: dt.timedelta
) -> PushMessage:
    """The push itself. `other_name` is the party who is not the recipient;
    `ttl` is what is left of the spec's, so a late send expires on time."""
    body = spec.body.format(name=other_name)
    return PushMessage(
        type=spec.type,
        title=spec.title,
        body=body[:1].upper() + body[1:],
        url=spec.path.format(session_id=session_id),
        tag=f"session:{session_id}",
        badge=unread,
        ttl=ttl,
        urgency=spec.urgency,
    )
