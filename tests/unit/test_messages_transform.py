"""The legacy chats, planned (decision 302): one chat per pair, every field
mapped, read marks from `seenRead`, and every row accounted for."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.enums import ConversationStatus
from app.domain.transform.messages import chat_roles, plan_messages

NEW_YORK = ZoneInfo("America/New_York")
ADA = "1700000000000x100000000000000001"
MO = "1700000000000x100000000000000002"
ZED = "1700000000000x100000000000000003"


def starter(anchor: str, by: str = MO, to: str = ADA, **extra: Any) -> dict[str, Any]:
    return {
        "unique id": anchor,
        "sendBy": by,
        "receiveBy": to,
        "messageRequestAccept": "yes",
        "lastMessageContent": "ignored",
        "Creation Date": "Jan 5, 2025 9:00 am",
        "Modified Date": "Jan 6, 2025 9:00 am",
    } | extra


def thread(anchor: str, chat: str, by: str = MO, **extra: Any) -> dict[str, Any]:
    return {
        "unique id": anchor,
        "messageStarter": chat,
        "sendBy": by,
        "receivedBy": "dropped",
        "messageContent": "  Hello there  ",
        "sentAt": "Jan 5, 2025 9:01 am",
        "Creation Date": "Jan 5, 2025 9:01 am",
        "Modified Date": "Jan 5, 2025 9:02 am",
        "seenRead": "no",
        "softDelete": "no",
    } | extra


def at(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=NEW_YORK)


def test_a_starter_and_its_messages_become_one_chat() -> None:
    plan = plan_messages(
        [starter("s1")],
        [
            thread("m1", "s1"),
            thread(
                "m2",
                "s1",
                by=ADA,
                messageContent="",
                sentAt="Jan 5, 2025 10:00 am",
                dateLastEdited="Jan 5, 2025 10:30 am",
                softDelete="yes",
                **{"Modified Date": "Jan 7, 2025 8:00 am"},
            ),
        ],
        timezone=NEW_YORK,
    )

    assert plan.quarantined == ()
    (chat,) = plan.chats
    assert chat.legacy_bubble_id == "s1"
    assert chat.people == tuple(sorted((ADA, MO)))
    assert chat.started_by == MO
    assert chat.status is ConversationStatus.ACCEPTED
    assert chat.last_message_at == at("2025-01-05 10:00")
    first, second = plan.messages
    assert (first.chat, first.sender, first.body) == ("s1", MO, "Hello there")
    assert first.sent_at == at("2025-01-05 09:01")
    assert (first.edited_at, first.deleted_at) == (None, None)
    # Blank is null; a soft delete is stamped when Bubble last touched it.
    assert second.body is None
    assert second.edited_at == at("2025-01-05 10:30")
    assert second.deleted_at == at("2025-01-07 08:00")
    assert plan.source_anchors == ("s1", "m1", "m2")


def test_two_starters_between_one_pair_merge_into_the_earliest() -> None:
    plan = plan_messages(
        [
            starter("late", by=ADA, to=MO, messageRequestAccept="yes"),
            starter("early", messageRequestAccept="no", **{"Creation Date": "Jan 1, 2025 9:00 am"}),
            starter("other", by=ZED, to=ADA),
        ],
        [thread("m1", "late", by=ADA), thread("m2", "early")],
        timezone=NEW_YORK,
    )

    assert len(plan.chats) == 2
    merged = next(chat for chat in plan.chats if chat.legacy_bubble_id == "early")
    assert merged.starters == ("early", "late")
    assert merged.started_by == MO
    assert merged.created_at == at("2025-01-01 09:00")
    # Any starter accepted makes the chat accepted.
    assert merged.status is ConversationStatus.ACCEPTED
    assert {message.chat for message in plan.messages} == {"early"}
    assert "1 starter(s) merged" in plan.report()


def test_a_chat_nobody_accepted_is_pending() -> None:
    plan = plan_messages([starter("s1", messageRequestAccept="")], [], timezone=NEW_YORK)

    assert plan.chats[0].status is ConversationStatus.PENDING
    assert plan.chats[0].last_message_at is None


def test_seen_moves_the_other_persons_read_mark_to_the_newest_seen() -> None:
    plan = plan_messages(
        [starter("s1")],
        [
            thread("m1", "s1", seenRead="yes", sentAt="Jan 5, 2025 9:01 am"),
            thread("m2", "s1", seenRead="yes", sentAt="Jan 5, 2025 9:30 am"),
            thread("m3", "s1", seenRead="no", sentAt="Jan 5, 2025 9:45 am"),
            thread("m4", "s1", by=ADA, seenRead="yes", sentAt="Jan 5, 2025 9:10 am"),
            # Out of order in the export: an older seen message never wins.
            thread("m5", "s1", seenRead="yes", sentAt="Jan 5, 2025 9:20 am"),
        ],
        timezone=NEW_YORK,
    )

    marks = {(mark.chat, mark.reader): mark.last_read_at for mark in plan.read_marks}
    assert marks == {("s1", ADA): at("2025-01-05 09:30"), ("s1", MO): at("2025-01-05 09:10")}


def test_sent_at_falls_back_to_creation_only_when_absent() -> None:
    plan = plan_messages(
        [starter("s1")],
        [thread("m1", "s1", sentAt="", **{"Creation Date": "Jan 5, 2025 11:00 am"})],
        timezone=NEW_YORK,
    )

    assert plan.messages[0].sent_at == at("2025-01-05 11:00")


def test_rows_that_cannot_be_written_are_quarantined_with_a_reason() -> None:
    plan = plan_messages(
        [
            starter("s1"),
            starter("s1"),
            starter("lonely", by=MO, to=""),
            starter("self", by=MO, to=MO),
        ],
        [
            thread("m1", "s1", by=ZED),
            thread("m2", "nowhere"),
            thread("m3", "lonely"),
            thread("m4", "s1", sentAt="not a date"),
            thread("m5", "s1"),
            thread("m5", "s1"),
        ],
        timezone=NEW_YORK,
    )

    reasons = {(row.legacy_bubble_id, row.reason) for row in plan.quarantined}
    assert reasons == {
        ("s1", "the export repeats this unique id"),
        ("lonely", "sendBy or receiveBy is absent"),
        ("self", "sendBy and receiveBy are the same person"),
        ("m1", "sendBy is not one of the two people in its chat"),
        ("m2", "messageStarter is absent or not loaded"),
        ("m3", "messageStarter is absent or not loaded"),
        ("m4", "sentAt is 'not a date', which is not a timestamp this export writes"),
        ("m5", "the export repeats this unique id"),
    }
    assert [message.legacy_bubble_id for message in plan.messages] == ["m5"]


def test_the_newest_booking_names_the_mentor_else_the_one_mentor_profile() -> None:
    pair = ("a", "b")

    assert chat_roles(pair, booked_mentor="b", profile_holders={"a", "b"}) == ("b", "a")
    assert chat_roles(pair, booked_mentor="a", profile_holders=set()) == ("a", "b")
    assert chat_roles(pair, booked_mentor=None, profile_holders={"b"}) == ("b", "a")
    assert chat_roles(pair, booked_mentor=None, profile_holders={"a"}) == ("a", "b")
    assert chat_roles(pair, booked_mentor=None, profile_holders={"a", "b"}) is None
    assert chat_roles(pair, booked_mentor=None, profile_holders=set()) is None
