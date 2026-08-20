"""Encrypting the one credential this service stores on somebody else's behalf.

**Every other secret here lives in configuration**; this one cannot. A mentor's
refresh token arrives per mentor, at a moment we do not choose, and has to
outlive the request — so it goes in the database, and the database is the one
place this project has not previously had to treat as hostile.

**Fernet rather than pgcrypto**, and the reason is where the key sits. pgcrypto
would put the key in the SQL statement, so it reaches the query log, the slow
query log, and any `pg_stat_statements` row that survives it. Encrypting in the
application keeps the key in the process and puts only ciphertext on the wire.

**`cryptography` is already a declared dependency**, so this costs nothing new —
which is most of why it is worth doing rather than deferring.

**One key, used for two things**, and that is a simplification with a stated
cost. It encrypts stored tokens *and* seals the OAuth `state` parameter, which
are different exposures: `state` travels through a browser and a redirect chain,
where a token never leaves the server. Separate keys would be the stricter
arrangement. They are shared because Fernet is authenticated encryption in both
cases — a leaked `state` reveals nothing and cannot be forged — and two secrets
to rotate for one trust boundary is the kind of operational cost that ends with
one of them never being rotated at all.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import AppError, ConfigurationError

__all__ = ["SealError", "seal", "sealed_value", "unseal", "unsealed_value"]

#: How long a sealed `state` stays valid, in seconds.
#:
#: **Ten minutes is a consent screen, not a session.** Long enough for somebody
#: to read what they are granting and pick an account; short enough that a
#: `state` captured from a browser history or a proxy log is useless by the time
#: anybody looks. Stored tokens are sealed without a TTL — they expire when the
#: mentor revokes them, not on a clock of ours.
STATE_TTL = 600


class SealError(AppError):
    """A sealed value could not be opened: tampered with, expired, or ours."""


def _cipher(key: str | None) -> Fernet:
    if not key:
        raise ConfigurationError("no encryption key is configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        # A malformed key is an operator fault caught at first use rather than
        # at import, which is where `ConfigurationError`'s own docstring says
        # settings failures belong.
        raise ConfigurationError("the encryption key is not a valid Fernet key") from exc


def seal(value: str, *, key: str | None) -> str:
    """Encrypt a string for storage. No TTL — it lives until it is replaced."""
    return _cipher(key).encrypt(value.encode()).decode()


def unseal(token: str, *, key: str | None) -> str:
    """Decrypt a stored string, or raise."""
    try:
        return _cipher(key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # **The likely cause is a rotated key, not an attack.** Saying so is the
        # difference between an operator checking their configuration and an
        # operator looking for an intruder.
        raise SealError("this value was sealed with a different key") from exc


def sealed_value(payload: dict[str, Any], *, key: str | None) -> str:
    """Seal a small object — the OAuth ``state``, and nothing else so far.

    JSON rather than a signed JWT, because there is no third party to interop
    with: only this service writes these and only this service reads them, so a
    standard buys nothing and costs a second way to get an algorithm wrong.
    """
    return seal(json.dumps(payload, sort_keys=True, separators=(",", ":")), key=key)


def unsealed_value(token: str, *, key: str | None, ttl: int = STATE_TTL) -> dict[str, Any]:
    """Open a sealed object, refusing one older than ``ttl``.

    **The TTL is enforced here rather than by a stored nonce**, which is the
    weaker of the two and the honest description: a `state` may be replayed
    within its ten minutes. What that buys an attacker is nothing — the code it
    accompanies is single-use at Google's end — so a nonce table would be a
    write per consent to close a window nothing can walk through.
    """
    try:
        opened = _cipher(key).decrypt(token.encode(), ttl=ttl)
    except InvalidToken as exc:
        raise SealError("this state is expired, tampered with, or not ours") from exc
    parsed = json.loads(opened)
    if not isinstance(parsed, dict):
        raise SealError("this state does not carry an object")
    return parsed
