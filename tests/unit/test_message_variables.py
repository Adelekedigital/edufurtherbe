"""Turning a session into the strings a template asked for.

**The failure this prevents is a delivered email, not a crash.** Loops accepts a
missing merge field and renders it as nothing, so an unresolved name arrives as
*"Hi , your session on "* — no error, no failed row, no way to notice. Every
refusal asserted here is a refusal chosen over that.

**The recipient is the unit, not the message.** A mentor in Lagos and a mentee in
Toronto are told the same instant in two different local times, and getting that
wrong produces a plausible email that sends somebody to the wrong hour.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.messages import (
    ALIASES,
    RESOLVERS,
    MessageContext,
    UnresolvedVariableError,
    build_variables,
)

LAGOS = "Africa/Lagos"  # UTC+1, no DST ever.
TORONTO = "America/Toronto"


def context(**overrides: object) -> MessageContext:
    base: dict[str, object] = {
        "recipient_name": "Ada Mentor",
        "recipient_timezone": LAGOS,
        "mentor_name": "Ada Mentor",
        "mentee_name": "Bo Mentee",
        "starts_at": dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC),
        "topic": "Personal statements",
        "detail": "I want help with my SOP",
        "venue": "Google Meet",
        "session_id": "01a0-session",
        "app_base_url": "https://app.edufurther.org",
    }
    return MessageContext(**(base | overrides))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The registry holds together
# --------------------------------------------------------------------------


def test_every_alias_points_at_a_real_resolver() -> None:
    """**A dangling alias is a template that fails for no visible reason.**

    Found this way, twice. `feedbacklink` aliased to `feedbackUrl` before any
    resolver existed; it read as supported and would have failed naming a
    variable nobody had heard of. The alias came back with the resolver and left
    again with it when the message was withdrawn — which is the second time this
    test earned itself, and the reason it is not a comment.
    """
    dangling = {name: target for name, target in ALIASES.items() if target not in RESOLVERS}

    assert dangling == {}


def test_no_alias_shadows_a_canonical_name() -> None:
    """An alias that is also a resolver name would make lookup order matter."""
    assert set(ALIASES) & set(RESOLVERS) == set()


# --------------------------------------------------------------------------
# Rendering per recipient
# --------------------------------------------------------------------------


def test_the_time_is_rendered_in_the_recipients_zone() -> None:
    """08:00Z is 09:00 in Lagos and 04:00 in Toronto — one instant, two emails."""
    lagos = build_variables(["sessionTime", "sessionTimezone"], context())
    toronto = build_variables(
        ["sessionTime", "sessionTimezone"], context(recipient_timezone=TORONTO)
    )

    assert lagos["sessionTime"] == "09:00"
    assert toronto["sessionTime"] == "04:00"
    assert (lagos["sessionTimezone"], toronto["sessionTimezone"]) == (LAGOS, TORONTO)


def test_a_date_can_differ_between_the_two_parties() -> None:
    """**The reason the zone travels with the time.** 00:30Z on the 2nd is still
    the 1st in Toronto, so the two parties are told different dates for one
    session — correctly, and unreadably without `sessionTimezone`."""
    at_midnight = context(starts_at=dt.datetime(2026, 9, 2, 0, 30, tzinfo=dt.UTC))

    lagos = build_variables(["sessionDate"], at_midnight)
    toronto = build_variables(
        ["sessionDate"], context(starts_at=at_midnight.starts_at, recipient_timezone=TORONTO)
    )

    assert "02 September" in lagos["sessionDate"]
    assert "01 September" in toronto["sessionDate"]


def test_the_other_party_is_whoever_the_recipient_is_not() -> None:
    """`attendee` is the one recipient-relative name the old templates use."""
    to_mentor = build_variables(["attendee"], context())
    to_mentee = build_variables(["attendee"], context(recipient_name="Bo Mentee"))

    assert to_mentor["attendee"] == "Bo Mentee"
    assert to_mentee["attendee"] == "Ada Mentor"


# --------------------------------------------------------------------------
# The old template names work untouched
# --------------------------------------------------------------------------


def test_a_session_confirmation_resolves_without_editing_loops() -> None:
    """The exact field list of the live Session Confirmation template.

    **This is what makes the rename optional.** Aliases carry the older
    generation onto the same resolvers, so nothing has to be edited in Loops
    before the first send.
    """
    declared = [
        "name",
        "sessiondate",
        "sessiontime",
        "attendee",
        "location",
        "topic",
        "topicDiscuss",
        "sessionlink",
    ]

    built = build_variables(declared, context())

    assert set(built) == set(declared), "a template gets back exactly what it asked for"
    assert built["name"] == "Ada Mentor"
    assert built["topicDiscuss"] == "I want help with my SOP"
    assert built["sessionlink"] == "https://app.edufurther.org/sessions/01a0-session"


def test_a_session_request_resolves_the_newer_names() -> None:
    """The newer generation, which needs no aliases at all."""
    built = build_variables(
        ["mentorName", "menteeName", "sessionDate", "sessionTopic", "discuss", "webUrl"],
        context(),
    )

    assert built["mentorName"] == "Ada Mentor"
    assert built["menteeName"] == "Bo Mentee"
    assert built["webUrl"].endswith("/sessions/01a0-session")


# --------------------------------------------------------------------------
# `sessionUrl` is the session page
# --------------------------------------------------------------------------


def test_the_link_is_the_session_page_and_carries_no_meeting_url() -> None:
    """**The decision most likely to be "fixed" wrongly later.**

    Meeting links are withheld until the join window opens five minutes before
    the start, so that nobody joins early and a Join press is something the
    platform can record. A meeting URL here would hand the room out days ahead.
    """
    built = build_variables(["sessionUrl", "location"], context(venue="Daily"))

    assert built["sessionUrl"] == "https://app.edufurther.org/sessions/01a0-session"
    # The venue is a label, never a URL.
    assert built["location"] == "Daily"
    assert "daily.co" not in built["sessionUrl"]


# --------------------------------------------------------------------------
# Refusals — each one chosen over a blank field
# --------------------------------------------------------------------------


def test_a_name_nothing_can_produce_is_refused_by_name() -> None:
    with pytest.raises(UnresolvedVariableError, match="mentorRating"):
        build_variables(["mentorRating"], context())


def test_an_empty_declaration_is_refused_as_unpublished() -> None:
    """**Loops reports no variables for a draft.** Treating that as "needs
    nothing" sends a blank email — the exact failure, wearing a disguise."""
    with pytest.raises(UnresolvedVariableError, match="unpublished"):
        build_variables([], context())


def test_a_missing_value_is_refused_rather_than_left_blank() -> None:
    """A session with no topic cannot fill a template that asks for one."""
    with pytest.raises(UnresolvedVariableError, match="sessionTopic"):
        build_variables(["sessionTopic"], context(topic=None))


def test_a_session_variable_on_a_message_with_no_session_is_refused() -> None:
    """`mentor_approved` has no session. A template asking for its date is a
    template pointed at the wrong message, and that should be loud."""
    no_session = MessageContext(
        recipient_name="Ada",
        recipient_timezone=LAGOS,
        mentor_name="Ada",
        mentee_name="",
    )

    with pytest.raises(UnresolvedVariableError, match="sessionDate"):
        build_variables(["sessionDate"], no_session)


def test_a_link_with_no_configured_app_is_refused() -> None:
    """Better than a link to `/sessions/…` with no host in front of it."""
    with pytest.raises(UnresolvedVariableError):
        build_variables(["sessionUrl"], context(session_id=None))


# --------------------------------------------------------------------------
# The values carried on the row
# --------------------------------------------------------------------------


def test_what_a_person_wrote_comes_from_the_event_not_the_session() -> None:
    """`reason_text` is a fact about the cancellation, unknowable afterwards
    from the session row."""
    built = build_variables(
        ["cancelmessage", "cancelinitiator"],
        context(extras={"reason_text": "I am unwell", "cancel_initiator": "Ada Mentor"}),
    )

    assert built["cancelmessage"] == "I am unwell"
    assert built["cancelinitiator"] == "Ada Mentor"


def test_hours_left_is_floored_and_never_negative() -> None:
    """A mentor told less time than they have answers sooner, which is the safe
    direction. Past the deadline it reads zero rather than a negative number."""
    ahead = context(respond_by=dt.datetime.now(dt.UTC) + dt.timedelta(hours=3, minutes=59))
    past = context(respond_by=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))

    assert build_variables(["hours"], ahead)["hours"] == "3"
    assert build_variables(["hours"], past)["hours"] == "0"
