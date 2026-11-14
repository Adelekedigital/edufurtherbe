"""Where a person and a booking came from (PRD 30 PR 2, decision 329).

**Acquisition is untrusted.** What a visitor arrived with (a UTM tag, a landing
page's guess) reaches us from their browser, so a value outside a vocabulary is
coerced, never refused: an unknown source is `direct`, an unknown channel
`other`. Refusing would lose the sign-up's attribution over a typo in a
campaign link, and a `422` on that is no use to anybody.

**`migrated` is ours alone.** Only the migration and the Bubble loader write it;
a visitor sending it is `direct`, like any other value they may not choose.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from app.domain.enums import AuthMethod, ConsentMethod, EntryPoint, SignupChannel, SignupSource

__all__ = [
    "CONSENT_VERSION_MAX_LENGTH",
    "auth_method_from",
    "entry_point_or_none",
    "method_fits",
    "signup_channel",
    "signup_source",
]


#: The longest consent-text version an analytics answer may name.
CONSENT_VERSION_MAX_LENGTH = 64


#: What a visitor may claim as their source: every source but `migrated`.
VISITOR_SOURCES = frozenset(SignupSource) - {SignupSource.MIGRATED}


def _member[E: StrEnum](enum_cls: type[E], raw: object) -> E | None:
    """``raw`` as a member of ``enum_cls``, case and spaces forgiven, or ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        return enum_cls(raw.strip().lower())
    except ValueError:
        return None


def signup_source(raw: object, *, invited: bool) -> SignupSource:
    """The source to record: `invite` when the code named a real invite,
    else the visitor's own if it is one they may claim, else `direct`."""
    if invited:
        return SignupSource.INVITE
    source = _member(SignupSource, raw)
    return source if source in VISITOR_SOURCES and source is not None else SignupSource.DIRECT


def signup_channel(raw: object) -> SignupChannel | None:
    """The channel to record: ``None`` when none was sent, `other` for any
    value outside the list."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _member(SignupChannel, raw) or SignupChannel.OTHER


def entry_point_or_none(raw: object) -> object:
    """A booking's entry point, or ``None`` for a value outside the list: a
    hint for analytics never fails a booking. A member passes through."""
    if raw is None or isinstance(raw, EntryPoint):
        return raw
    return _member(EntryPoint, raw)


#: The one answer each consent method can give, ``None`` for either. Every
#: member is listed, so a new one fails here until somebody decides.
_METHOD_ANSWER: dict[ConsentMethod, bool | None] = {
    ConsentMethod.ACCEPT: True,
    ConsentMethod.DECLINE: False,
    ConsentMethod.CLOSE: False,
    ConsentMethod.SETTINGS: None,
}


def method_fits(method: ConsentMethod | None, *, granted: bool) -> bool:
    """Whether ``method`` could have given this answer (decision 345); no
    method at all fits either."""
    if method is None:
        return True
    answer = _METHOD_ANSWER[method]
    return answer is None or answer == granted


#: Supabase's `amr` methods that mean a code sent to the address
#: (https://supabase.com/docs/guides/auth/jwt-fields, "amr").
_EMAIL_METHODS = frozenset({"otp", "magiclink", "email/signup"})


def auth_method_from(methods: Iterable[str], providers: Iterable[str]) -> AuthMethod | None:
    """How this sign-in signed in, from the token's `amr` methods and
    `app_metadata.providers`, or ``None`` when it says nothing we name.

    `oauth` is Google only when Google is among the account's providers: no
    other OAuth provider is switched on, and one added later must not be
    counted as Google.
    """
    said = set(methods)
    if "oauth" in said and "google" in set(providers):
        return AuthMethod.GOOGLE_OAUTH
    if said & _EMAIL_METHODS:
        return AuthMethod.EMAIL_CODE
    return None
