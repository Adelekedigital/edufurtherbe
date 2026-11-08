"""Web Push: a browser's subscription to this platform's notifications (#386).

The bounds live here once: the columns' `CHECK`s read them (and the request
models will), and a migration test pins the live constraints to them.

A subscription is what `PushSubscription.toJSON()` gives a browser (RFC 8030
for the endpoint, RFC 8291 for the keys): the push service's URL for one
browser profile, and the two keys a payload is encrypted to.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.core.config import BASE64URL_PATTERN

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
