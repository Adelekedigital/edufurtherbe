"""How an email address is stored: trimmed and lowercased.

**One function, because the database holds everyone to it.** `users.email` is
plain `text` with `CHECK (email = lower(email))` and a unique index on live
rows, so an address normalised any other way is either refused by the CHECK —
a 500 — or slips past the index as a second account for the same person. It
was typed out four times before this (profile writes, referrals, the Bubble
transform, first sign-in); the copy that drifted would have been silent.
"""

from __future__ import annotations

__all__ = ["calendar_invite_address", "is_gmail", "normalise_email"]

#: Gmail's own domains, where "+" starts an alias of the same account.
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalise_email(value: str) -> str:
    """The stored form of an address."""
    return value.strip().lower()


def calendar_invite_address(value: str) -> str:
    """The address a calendar invite is sent to (decision 357).

    Google Meet admits a guest without knocking only when the Google account
    they are signed in to matches an invited address. A Gmail ``+`` alias
    (``ada+edu@gmail.com``) reaches the same inbox but is not the account's
    address, so the guest waits to be admitted. So on Gmail's own domains
    everything from the first ``+`` in the local part is dropped.

    **Dots and the domain are left as they are**: an address that admits
    people today keeps matching exactly, and another provider may treat
    ``+`` as part of a distinct address. One rule, read by the invite and by
    `SessionRead.meet_invite_email`, so what a party is told is what Google
    was sent.
    """
    address = normalise_email(value)
    local, at, domain = address.rpartition("@")
    if not at or domain not in _GMAIL_DOMAINS:
        return address
    account = local.split("+", 1)[0]
    # Nothing before the "+" is no account to name: left as stored.
    return f"{account}@{domain}" if account else address


def is_gmail(address: str) -> bool:
    """Whether ``address`` is on Gmail's own domains, so is a Google account
    by construction. Another domain may or may not be one (Workspace is, a
    Yahoo address usually is not), and nothing here can tell."""
    return normalise_email(address).rpartition("@")[2] in _GMAIL_DOMAINS
