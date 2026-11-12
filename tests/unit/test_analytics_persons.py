"""What a PostHog person page shows about somebody (decision 340): ids, enums
and a month, never a name, an address or free text."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from app.domain.analytics_events import (
    AnalyticsEvent,
    analytics_event,
    mentor_status,
    person_properties,
    person_update,
    with_person,
)
from app.domain.enums import (
    ApprovalStatus,
    ListingStatus,
    PrimaryRole,
    SignupChannel,
    SignupSource,
)

USER = uuid4()
CREATED = dt.datetime(2025, 3, 31, 23, 30, tzinfo=dt.UTC)


def person(**overrides: object) -> dict[str, dict[str, object]]:
    facts: dict[str, object] = {
        "user_id": USER,
        "role": PrimaryRole.MENTEE,
        "is_migrated": False,
        "signup_source": SignupSource.CAMPAIGN,
        "signup_channel": SignupChannel.LINKEDIN,
        "created_at": CREATED,
        "approval_status": None,
        "listing_status": None,
    }
    return person_properties(**{**facts, **overrides})  # type: ignore[arg-type]


def test_a_mentee_carries_its_current_state_and_no_mentor_status() -> None:
    assert person() == {
        "$set": {
            "user_id": str(USER),
            "role": "mentee",
            "is_migrated": False,
            "signup_source": "campaign",
            "signup_channel": "linkedin",
        },
        "$set_once": {"account_created_month": "2025-03"},
        "$unset": ["mentor_status"],
    }


def test_a_mentor_keeps_its_status_and_clears_nothing() -> None:
    found = person(approval_status=ApprovalStatus.APPROVED, listing_status=ListingStatus.LISTED)

    assert "$unset" not in found


def test_a_migrated_mentee_with_no_channel_says_so_with_null() -> None:
    found = person(is_migrated=True, signup_source=SignupSource.MIGRATED, signup_channel=None)

    assert found["$set"]["is_migrated"] is True
    assert found["$set"]["signup_source"] == "migrated"
    assert found["$set"]["signup_channel"] is None
    assert "mentor_status" not in found["$set"]


def test_unset_source_and_channel_are_null() -> None:
    found = person(signup_source=None, signup_channel=None)

    assert (found["$set"]["signup_source"], found["$set"]["signup_channel"]) == (None, None)


def test_an_approved_listed_mentor_is_listed() -> None:
    found = person(
        role=PrimaryRole.MENTOR,
        approval_status=ApprovalStatus.APPROVED,
        listing_status=ListingStatus.LISTED,
    )

    assert found["$set"]["role"] == "mentor"
    assert found["$set"]["mentor_status"] == "listed"


def test_a_pending_mentor_is_pending_whatever_its_listing() -> None:
    found = person(
        role=PrimaryRole.MENTOR,
        approval_status=ApprovalStatus.PENDING,
        listing_status=ListingStatus.UNLISTED,
    )

    assert found["$set"]["mentor_status"] == "pending"


@pytest.mark.parametrize(
    ("approval", "listing", "status"),
    [
        ("approved", "listed", "listed"),
        ("approved", "unlisted", "unlisted"),
        ("pending", "listed", "pending"),
        ("declined", "unlisted", "declined"),
    ],
)
def test_the_mentor_status_is_the_listing_once_approved_else_the_approval(
    approval: str, listing: str, status: str
) -> None:
    assert mentor_status(approval, listing) == status


def test_the_creation_month_is_the_utc_month_only() -> None:
    """23:30 UTC on 31 March is already April in Lagos: the UTC month is kept."""
    lagos = CREATED.astimezone(dt.timezone(dt.timedelta(hours=1)))

    assert person(created_at=lagos)["$set_once"] == {"account_created_month": "2025-03"}


def test_a_creation_time_without_a_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="zone"):
        person(created_at=CREATED.replace(tzinfo=None))


def test_the_person_is_guarded_at_the_source_and_merged_as_it_is() -> None:
    """Guarded once, in `person_properties`: ids and enums are already plain
    text there, and the merges add exactly that, untouched."""
    guarded = person(role=PrimaryRole.MENTEE)
    event = analytics_event(AnalyticsEvent.CREDIT_SPENT, user_id=USER, at=CREATED, properties={})

    merged = with_person(event, guarded)
    update = person_update(USER, guarded)

    assert type(guarded["$set"]["user_id"]) is str and type(guarded["$set"]["role"]) is str
    assert {k: merged["properties"][k] for k in guarded} == guarded
    assert merged["properties"]["$geoip_disable"] is True
    assert "$set" not in event["properties"]
    assert update == {
        "event": "$set",
        "distinct_id": str(USER),
        "properties": {**guarded, "$geoip_disable": True},
    }


@pytest.mark.parametrize("key", ["email", "first_name", "Contact_Email", "about_me"])
def test_a_personal_key_smuggled_into_a_person_update_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="personal"):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT, user_id=USER, at=CREATED, properties={"$set": {key: "x"}}
        )


def test_free_text_smuggled_into_a_person_update_is_refused() -> None:
    with pytest.raises(ValueError, match="id, enum value or code"):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT,
            user_id=USER,
            at=CREATED,
            properties={"$set_once": {"school": "ada@example.com"}},
        )


@pytest.mark.parametrize(
    ("names", "why"),
    [(["email"], "personal"), (["two words"], "id, enum value or code"), ([3], "names")],
)
def test_unset_lists_plain_impersonal_names_only(names: list[object], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT, user_id=USER, at=CREATED, properties={"$unset": names}
        )


def test_a_list_anywhere_but_unset_is_refused() -> None:
    with pytest.raises(ValueError, match="id, enum, count or code"):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT,
            user_id=USER,
            at=CREATED,
            properties={"$set": {"$unset": ["role"]}},
        )


def test_a_nested_mapping_in_an_event_goes_through_the_guard_too() -> None:
    with pytest.raises(ValueError, match="personal"):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT,
            user_id=USER,
            at=CREATED,
            properties={"$set": {"name": "A"}},
        )


def test_a_mapping_deeper_than_a_person_update_is_refused() -> None:
    with pytest.raises(ValueError, match="id, enum, count or code"):
        analytics_event(
            AnalyticsEvent.CREDIT_SPENT,
            user_id=USER,
            at=CREATED,
            properties={"$set": {"$set": {"role": "mentee"}}},
        )


def test_me_reads_is_migrated_from_the_one_predicate() -> None:
    from app.api.deps import core as deps

    assert deps._MIGRATED.params == {}
    assert "(u.signup_source = 'migrated') AS is_migrated" in str(deps.CURRENT_USER)


def test_the_reader_keeps_only_the_asked_ids_posthog_answers() -> None:
    """Bounded to our ids, read back as ids: a stray value is dropped."""
    import httpx
    from tests.posthog_fakes import body_of, posthog_reader, rows

    known, unknown, internal = uuid4(), uuid4(), uuid4()
    reader, seen = posthog_reader(
        lambda _: rows(["distinct_id"], [str(known)], ["anonymous-browser-id"])
    )

    assert reader.known_persons([known, unknown], [internal]) == {known}
    (request,) = seen
    query = body_of(request)["query"]
    assert sorted(query["values"]["ids"]) == sorted([str(known), str(unknown)])
    assert query["values"]["internal"] == [str(internal)]
    assert "{ids}" in query["query"]
    assert isinstance(request, httpx.Request)


def test_the_reader_asks_nothing_for_no_ids() -> None:
    from tests.posthog_fakes import posthog_reader, rows

    reader, seen = posthog_reader(lambda _: rows(["distinct_id"]))

    assert reader.known_persons([], []) == set()
    assert seen == []
