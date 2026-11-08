"""The users transform: an empty role and a non-ASCII slug (decision 311).

The production export (stamped 2026-11-08 UTC) refused 212 users for an empty
``Role`` and 3 for an accented slug. 210 of the 212 never finished onboarding;
the owner's rule is that they load like any new sign-up, with the column's
default hint and onboarding left as Bubble has it, so the app sends them to
onboarding. A role that is set but unknown still refuses.
"""

from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import USER_ID
from tests.conftest import user as legacy_user

from app.domain.enums import PrimaryRole
from app.domain.transform import TransformError, to_user, transform_users
from app.domain.transform.identity import (
    ONBOARDING_COMPLETED_FIELD,
    ROLE_FIELD,
    UNCHOSEN_ROLE,
    to_onboarding,
    to_profile,
)
from app.domain.transform.profiles import MENTOR_LINK_FIELD, unchosen_mentor_owners
from app.infra.db.models.user import User

SECOND = {"bubble_id": "2x2", "email": "person.two@example.com"}


def record(**overrides: Any) -> dict[str, Any]:
    """The suite's one canonical legacy user, with a slug to fold."""
    return legacy_user(Slug="person-one") | overrides


# --- the role ---------------------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_role_loads_as_the_unchosen_hint(empty: str | None) -> None:
    assert to_user(record(**{ROLE_FIELD: empty})).primary_role is UNCHOSEN_ROLE


def test_a_missing_role_field_loads_as_the_unchosen_hint() -> None:
    raw = record()
    del raw[ROLE_FIELD]
    assert to_user(raw).primary_role is UNCHOSEN_ROLE


@pytest.mark.parametrize(
    ("raw", "role"), [("Mentee", PrimaryRole.MENTEE), ("Mentor", PrimaryRole.MENTOR)]
)
def test_a_chosen_role_is_kept(raw: str, role: PrimaryRole) -> None:
    assert to_user(record(**{ROLE_FIELD: raw})).primary_role is role


def test_a_set_but_unknown_role_still_refuses() -> None:
    with pytest.raises(TransformError, match="unmapped role"):
        to_user(record(**{ROLE_FIELD: "Coach"}))


def test_the_unchosen_hint_is_the_columns_own_default() -> None:
    """One value, two places (non-negotiable #8): the transform's default and
    ``users.primary_role``'s server default must not drift apart. The migrated
    column is checked too, in ``test_identity_load``."""
    default = User.__table__.c.primary_role.server_default
    assert default is not None
    assert str(default.arg) == f"'{UNCHOSEN_ROLE.value}'"


def test_an_empty_role_keeps_onboarding_unfinished() -> None:
    """What sends them to onboarding is the missing completion, not the role."""
    raw = record(**{ROLE_FIELD: "", ONBOARDING_COMPLETED_FIELD: None})
    assert to_onboarding(raw).completed_at is None


def test_an_empty_role_mentor_owner_is_counted() -> None:
    """They keep the mentee hint, so the dry run names them."""
    owner = record(**{ROLE_FIELD: "", MENTOR_LINK_FIELD: "1700000000000x900000000000000009"})
    chosen = record(
        **SECOND, **{ROLE_FIELD: "Mentor", MENTOR_LINK_FIELD: "1700000000000x900000000000000010"}
    )
    no_mentor = record(bubble_id="3x3", email="three@example.com", **{ROLE_FIELD: ""})

    assert unchosen_mentor_owners([owner, chosen, no_mentor]) == (USER_ID,)


# --- the slug ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("bánkólé-wright", "bankole-wright"),
        ("Ade-OLA", "ade-ola"),
        ("person-one", "person-one"),
        ("", None),
        ("  ", None),
    ],
)
def test_a_slug_is_folded_to_url_safe_ascii(raw: str, stored: str | None) -> None:
    assert to_user(record(Slug=raw)).slug == stored


@pytest.mark.parametrize(
    "raw",
    [
        "has spaces",
        "slash/in-it",
        "山田",
        # A letter with no ASCII form is refused, never dropped: these would
        # otherwise load as "sren", "strae" and "-taro".
        "søren",
        "straße",
        "山田-taro",
    ],
)
def test_a_slug_that_folding_cannot_fix_still_refuses(raw: str) -> None:
    with pytest.raises(TransformError, match="not URL-safe"):
        to_user(record(Slug=raw))


def test_a_refused_slug_is_not_echoed() -> None:
    """A slug is usually a name; the operator's report carries ids, not values."""
    with pytest.raises(TransformError) as refused:
        to_user(record(Slug="søren-hansen"))
    assert "hansen" not in str(refused.value)


def test_a_shared_slug_is_found_before_the_load() -> None:
    """The partial unique index would reject the second one halfway through a
    load; folding makes a collision possible between two different raw slugs."""
    report = transform_users([record(Slug="bánkólé"), record(**SECOND, Slug="bankole")])

    assert not report.ok
    assert not report.errors, "a shared slug blocks the load; it refuses no record"
    assert report.duplicate_slugs == ((USER_ID, "2x2"),)


def test_users_without_a_slug_never_share_one() -> None:
    """344 production users have no slug; no slug is nobody's link."""
    report = transform_users([record(Slug=None), record(**SECOND, Slug=None)])
    assert report.ok, report.errors


def test_distinct_slugs_pass() -> None:
    report = transform_users([record(), record(**SECOND, Slug="person-two")])
    assert report.ok


# --- languages (decision 316) ----------------------------------------------


def test_a_comma_joined_single_language_field_is_split() -> None:
    """The production export joins several names into the "single" field too."""
    profile = {
        "bubble_id": "1700000000000x000000000000000050",
        "created_at": "Sep 3, 2025 5:31 am",
        "modified_at": "Sep 4, 2025 5:31 am",
        "list-Language": "",
        "Language": "English , Yoruba",
    }
    row = to_profile(
        profile,
        user_bubble_id=USER_ID,
        user_record=record(),
        export_timezone=ZoneInfo("America/New_York"),
    )
    assert row.language_names == ("English", "Yoruba")
