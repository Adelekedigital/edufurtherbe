"""Web Push: a browser's subscription to this platform's notifications (#386).

The bounds live here once: the columns' `CHECK`s read them (and the request
models will), and a migration test pins the live constraints to them.

A subscription is what `PushSubscription.toJSON()` gives a browser (RFC 8030
for the endpoint, RFC 8291 for the keys): the push service's URL for one
browser profile, and the two keys a payload is encrypted to.
"""

from __future__ import annotations

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
