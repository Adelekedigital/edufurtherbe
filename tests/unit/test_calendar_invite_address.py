"""The address a calendar invite is sent to (decision 357).

Google Meet admits a guest without knocking only when the Google account they
are signed in to matches an invited address. A Gmail "+" alias is the same
inbox but not the same account, so it is invited as the plain address. Dots and
the googlemail.com domain are left alone: an address that matches today keeps
matching.
"""

from __future__ import annotations

import pytest

from app.domain.emails import calendar_invite_address
from app.domain.meetings import meet_invite_for, meet_link_for


@pytest.mark.parametrize(
    ("stored", "invited"),
    [
        ("ada+edu@gmail.com", "ada@gmail.com"),
        ("ada+edu+2026@gmail.com", "ada@gmail.com"),
        ("ada.obi+edu@gmail.com", "ada.obi@gmail.com"),
        ("ada+@gmail.com", "ada@gmail.com"),
        ("ada+edu@googlemail.com", "ada@googlemail.com"),
        ("Ada+Edu@Gmail.com", "ada@gmail.com"),
        ("  ada+edu@gmail.com ", "ada@gmail.com"),
    ],
)
def test_a_gmail_plus_alias_is_invited_as_the_account(stored: str, invited: str) -> None:
    assert calendar_invite_address(stored) == invited


@pytest.mark.parametrize(
    "stored",
    [
        "ada@gmail.com",
        # Dots name the same Gmail account, but the account may be registered
        # with them; changing them could break an invite that works today.
        "ada.obi@gmail.com",
        "ada@googlemail.com",
        # Another provider may treat "+" as part of a distinct address.
        "ada+edu@school.edu",
        "ada+edu@outlook.com",
        "ada+edu@gmail.com.ng",
        "tunde@workspace.example",
        # Nothing before the "+" names no account, so nothing is dropped.
        "+edu@gmail.com",
    ],
)
def test_every_other_address_is_invited_as_stored(stored: str) -> None:
    assert calendar_invite_address(stored) == stored


def test_an_address_with_no_at_sign_is_returned_normalised_not_refused() -> None:
    assert calendar_invite_address(" Not-An-Address ") == "not-an-address"


# --------------------------------------------------------------------------
# The Meet link a party is handed opens under their invited account
# --------------------------------------------------------------------------

MEET = "https://meet.google.com/abc-defg-hij"


def test_the_link_names_the_invited_gmail_account() -> None:
    assert meet_link_for(MEET, "ada@gmail.com") == f"{MEET}?authuser=ada%40gmail.com"


@pytest.mark.parametrize(
    ("provider", "address", "invited"),
    [
        ("google_meet", "ada+edu@gmail.com", "ada@gmail.com"),
        ("google_meet", "tunde@school.edu", "tunde@school.edu"),
        ("google_meet", None, None),
        ("daily", "ada@gmail.com", None),
        ("custom", "ada@gmail.com", None),
        (None, "ada@gmail.com", None),
    ],
)
def test_only_a_meet_party_has_an_invited_address(
    provider: str | None, address: str | None, invited: str | None
) -> None:
    assert meet_invite_for(provider, address) == invited


def test_an_existing_query_is_kept_blanks_included_and_an_existing_account_replaced() -> None:
    assert (
        meet_link_for(f"{MEET}?hs=122&x=&authuser=0", "ada@gmail.com")
        == f"{MEET}?hs=122&x=&authuser=ada%40gmail.com"
    )


@pytest.mark.parametrize("address", ["tunde@school.edu", "ada@yahoo.com", "ada@outlook.com"])
def test_an_address_that_may_not_be_a_google_account_is_not_named(address: str) -> None:
    """Naming it would send them to a sign-in for an account that may not exist."""
    assert meet_link_for(MEET, address) == MEET


def test_a_googlemail_address_is_named() -> None:
    assert meet_link_for(MEET, "ada@googlemail.com") == f"{MEET}?authuser=ada%40googlemail.com"


def test_no_address_leaves_the_link_as_it_is() -> None:
    assert meet_link_for(MEET, None) == MEET
    assert meet_link_for(MEET, "") == MEET
