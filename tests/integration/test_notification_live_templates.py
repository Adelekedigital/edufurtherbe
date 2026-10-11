"""Real events, drained, against what the live Loops templates ask for.

Found on dev (2026-10-03): `session_requested` failed on `sessionTopic` for a
booking with no topic, and `request_declined` failed on `reasonTitle` for a
decline with no code, so mentors were not told of requests and mentees were not
told of declines. These drain real rows through the real context loader and
build the live templates' variables from them.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_notifications import Recorder, a_booking, sweep
from tests.unit.test_template_registry import live_variables

from app.domain.enums import SessionReasonCode
from app.domain.messages import REASON_TITLES, build_variables
from app.domain.notifications import Notification
from conftest import A_REASON

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def sent(recorder: Recorder, notification: Notification) -> list[dict[str, str]]:
    return [
        build_variables(
            live_variables()[notification.value],
            # The test settings configure no origin, and links refuse without one.
            replace(message["context"], app_base_url="https://app.example.test"),
        )
        for message in recorder.sent
        if message["notification"] == notification
    ]


async def test_a_request_with_no_topic_reaches_the_mentor_named_by_its_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await a_booking(db_engine, api_client, "lt-request", confirmation=True)
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (variables,) = sent(recorder, Notification.SESSION_REQUESTED)
    assert variables["sessionTopic"] == "General Mentorship"
    assert variables["discuss"] == ""


async def test_a_decline_with_only_a_code_reaches_the_mentee(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A code is required now (decision 237), so the bare minimum a decline
    carries is one, with no message. The "No reason given" fallback is for older
    events and is pinned in `test_message_variables`."""
    booking = await a_booking(db_engine, api_client, "lt-decline", confirmation=True)
    declined = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/decline",
        json=A_REASON,
        headers=booking["mentor_headers"],
    )
    assert declined.status_code in (200, 204), declined.text
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (variables,) = sent(recorder, Notification.REQUEST_DECLINED)
    assert variables["reasonTitle"] == REASON_TITLES[SessionReasonCode.OTHER]
    assert variables["reasonMessage"] == ""


async def test_a_coded_decline_reads_the_code_in_words(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "lt-coded", confirmation=True)
    await api_client.post(
        f"/api/v1/sessions/{booking['id']}/decline",
        json={"reason_code": "scheduling_conflict", "reason_text": "Clash"},
        headers=booking["mentor_headers"],
    )
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (variables,) = sent(recorder, Notification.REQUEST_DECLINED)
    assert variables["reasonTitle"] == REASON_TITLES[SessionReasonCode.SCHEDULING_CONFLICT]
    assert variables["reasonMessage"] == "Clash"


async def test_a_cancellation_names_who_called_it_off(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await a_booking(db_engine, api_client, "lt-cancel")
    cancelled = await api_client.post(
        f"/api/v1/sessions/{booking['id']}/cancel", json=A_REASON, headers=booking["mentee_headers"]
    )
    assert cancelled.status_code in (200, 204), cancelled.text
    recorder = Recorder()

    await sweep(db_engine, recorder)

    (variables,) = sent(recorder, Notification.SESSION_CANCELLED)
    assert variables["cancelinitiator"] == "Mo"
    assert variables["cancelmessage"] == ""
