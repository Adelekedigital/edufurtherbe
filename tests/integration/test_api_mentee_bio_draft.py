"""The silent mentee bio draft (Onboarding v5 B6, decision 269).

Two halves. **Completion queues it**: a mentee's first completion publishes one
QStash callback with no delay, and nothing else does (a mentor, a retry, the
flag off, the assistant unconfigured). **The callback writes it**, and only into
an empty bio: the person's own words always win, including words typed while
the router was answering. The fakes stand on `app.state.scheduler` and
`app.state.writing_assistant`, the seams the code reads.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.enums import WritingAssistMode
from app.domain.writing_assist import (
    BACKGROUND_ATTEMPT,
    TASKS,
    AssistCall,
    AssistResult,
    Task,
)
from app.infra.clients.scheduler import SchedulerError
from conftest import add_user, api_token, bearer, give_mentee_answers

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

COMPLETION = "/api/v1/me/onboarding/completion"
PATH = "/api/v1/callbacks/bio-drafts"
BASE = "https://api.example.test"
SIGNING_KEY = "not-a-real-signing-key-for-local-tests"
DRAFT = "A curious student from Nigeria, aiming for Canada."


@dataclass
class FakeScheduler:
    published: list[dict[str, Any]] = field(default_factory=list)
    fails: bool = False

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        if self.fails:
            raise SchedulerError("qstash said no")
        self.published.append({"url": url, "body": body, "at": at})


@dataclass
class FakeAssistant:
    reply: AssistResult | None = field(
        default_factory=lambda: AssistResult(text=f"  {DRAFT}  ", prompt_version="bio_draft-v1")
    )
    available: bool = True
    calls: list[AssistCall] = field(default_factory=list)
    #: Runs inside the call, as a person typing while the router answers would.
    during: Callable[[], None] | None = None

    def run(self, call: AssistCall) -> AssistResult | None:
        self.calls.append(call)
        if self.during is not None:
            self.during()
        return self.reply


@dataclass
class World:
    client: httpx.AsyncClient
    engine: AsyncEngine
    scheduler: FakeScheduler
    assistant: FakeAssistant
    app: Any

    def configure(self, **update: Any) -> None:
        self.app.state.settings = self.app.state.settings.model_copy(update=update)


@pytest.fixture
def world(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> World:
    app = api_client._transport.app  # type: ignore[attr-defined]
    scheduler, assistant = FakeScheduler(), FakeAssistant()
    app.state.scheduler = scheduler
    app.state.writing_assistant = assistant
    built = World(api_client, db_engine, scheduler, assistant, app)
    built.configure(
        mentee_bio_draft_enabled=True,
        public_base_url=BASE,
        qstash_current_signing_key=SecretStr(SIGNING_KEY),
    )
    return built


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`caplog`, with the logger read here switched back on: Alembic's
    `fileConfig` disables every logger that existed when the schema migrated."""
    monkeypatch.setattr(logging.getLogger("app.api.deps.core"), "disabled", False)
    return caplog


@dataclass
class Person:
    id: UUID
    email: str
    headers: dict[str, str]


async def a_mentee(
    engine: AsyncEngine, *, about_me: str | None = None, mentor: bool = False, goal: bool = True
) -> Person:
    auth_id = uuid4()
    email = f"bio-{uuid4().hex[:8]}@example.test"
    async with engine.begin() as conn:
        user_id = await add_user(
            conn,
            email,
            auth_id=auth_id,
            first_name="Adaeze",
            last_name="Okonkwo",
            primary_role="mentee",
            timezone="UTC",
        )
        await conn.execute(
            text("INSERT INTO user_profiles (user_id, about_me) VALUES (:u, :a)"),
            {"u": user_id, "a": about_me},
        )
        if goal:
            await conn.execute(
                text("INSERT INTO mentee_goals (user_id) VALUES (:u)"), {"u": user_id}
            )
            await give_mentee_answers(conn, user_id)
        if mentor:
            await conn.execute(
                text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Mentor')"),
                {"u": user_id},
            )
    return Person(UUID(str(user_id)), email, bearer(api_token(auth_id)))


async def bio_of(engine: AsyncEngine, person: Person) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT about_me, about_me_drafted_at, about_me_prompt_version "
                    "FROM user_profiles WHERE user_id = :u"
                ),
                {"u": person.id},
            )
        ).one()
    return dict(row._mapping)


async def run_sql(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


def signed(body: bytes, *, key: str = SIGNING_KEY, path: str = PATH) -> dict[str, str]:
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {
            "iss": "Upstash",
            "sub": f"{BASE}{path}",
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "nbf": int(now.timestamp()),
            "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode(),
        },
        key,
        algorithm="HS256",
    )
    return {"Upstash-Signature": token, "Content-Type": "application/json"}


async def deliver(world: World, person: Person) -> httpx.Response:
    body = json.dumps({"user_id": str(person.id)}).encode()
    return await world.client.post(PATH, content=body, headers=signed(body))


# --------------------------------------------------------------------------
# Completion queues it
# --------------------------------------------------------------------------


async def test_a_mentees_first_completion_queues_one_draft_with_no_delay(world: World) -> None:
    person = await a_mentee(world.engine)
    before = dt.datetime.now(dt.UTC)

    response = await world.client.post(COMPLETION, headers=person.headers)

    assert response.status_code == 201, response.text
    assert len(world.scheduler.published) == 1
    published = world.scheduler.published[0]
    assert published["url"] == f"{BASE}{PATH}"
    assert published["body"] == {"user_id": str(person.id)}
    assert before <= published["at"] <= dt.datetime.now(dt.UTC)


async def test_a_retried_completion_queues_nothing_more(world: World) -> None:
    person = await a_mentee(world.engine)
    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201

    retry = await world.client.post(COMPLETION, headers=person.headers)

    assert retry.status_code == 200
    assert len(world.scheduler.published) == 1


async def test_a_completion_that_grants_nothing_queues_nothing(world: World) -> None:
    """Already holding the starter, never marked complete: a `200`, not a first."""
    person = await a_mentee(world.engine)
    await run_sql(
        world.engine,
        "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining, "
        "expires_at) VALUES (:u, 'profile_completed', 1, 1, NULL)",
        u=person.id,
    )

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 200
    assert world.scheduler.published == []


async def test_an_account_finished_before_queues_nothing(world: World) -> None:
    """Marked complete by the ETL with no starter: the credit is paid now
    (`201`), but this is not their first completion, so no bio appears."""
    person = await a_mentee(world.engine)
    await run_sql(
        world.engine,
        "INSERT INTO user_onboarding (user_id, completed_at) "
        "VALUES (:u, now() - interval '90 days')",
        u=person.id,
    )

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201
    assert world.scheduler.published == []


async def test_a_mentor_only_completion_queues_nothing(world: World) -> None:
    person = await a_mentee(world.engine, mentor=True, goal=False)

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201
    assert world.scheduler.published == []


async def test_a_mentor_with_a_goal_queues_nothing(world: World) -> None:
    """Their bio is a mentor's public "about"; a mentee draft must not fill it."""
    person = await a_mentee(world.engine, mentor=True)

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201
    assert world.scheduler.published == []


@pytest.mark.parametrize(
    "update",
    [{"mentee_bio_draft_enabled": False}, {"public_base_url": None}],
    ids=["flag-off", "no-callback-url"],
)
async def test_off_or_unaddressable_queues_nothing(world: World, update: dict[str, Any]) -> None:
    world.configure(**update)
    person = await a_mentee(world.engine)

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201
    assert world.scheduler.published == []


async def test_an_unconfigured_assistant_queues_nothing(world: World) -> None:
    world.assistant.available = False
    person = await a_mentee(world.engine)

    assert (await world.client.post(COMPLETION, headers=person.headers)).status_code == 201
    assert world.scheduler.published == []


async def test_a_failed_publish_does_not_fail_completion(
    world: World, logs: pytest.LogCaptureFixture
) -> None:
    world.scheduler.fails = True
    person = await a_mentee(world.engine)

    with logs.at_level(logging.WARNING):
        response = await world.client.post(COMPLETION, headers=person.headers)

    assert response.status_code == 201
    assert response.json()["completed_at"] is not None
    assert any(r.getMessage() == "mentee bio draft not queued" for r in logs.records)


# --------------------------------------------------------------------------
# The callback is QStash's alone
# --------------------------------------------------------------------------


async def test_an_unsigned_delivery_is_refused(world: World) -> None:
    person = await a_mentee(world.engine)
    body = json.dumps({"user_id": str(person.id)}).encode()

    response = await world.client.post(PATH, content=body)

    assert response.status_code == 401
    assert world.assistant.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        lambda body: signed(body, key="somebody-elses-signing-key-for-these-tests"),
        lambda _body: signed(b'{"user_id": "someone else"}'),
        lambda body: signed(body, path="/api/v1/callbacks/reminders"),
    ],
    ids=["wrong-key", "wrong-body", "wrong-endpoint"],
)
async def test_a_badly_signed_delivery_is_refused(
    world: World, headers: Callable[[bytes], dict[str, str]]
) -> None:
    person = await a_mentee(world.engine)
    body = json.dumps({"user_id": str(person.id)}).encode()

    response = await world.client.post(PATH, content=body, headers=headers(body))

    assert response.status_code == 401
    assert world.assistant.calls == []
    assert (await bio_of(world.engine, person))["about_me"] is None


async def test_an_unconfigured_verifier_refuses_everything(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused by the configuration check itself: with the signature check
    stubbed to accept anything, no key still means no entry."""
    monkeypatch.setattr("app.api.deps.jobs.verify_callback", lambda **_: None)
    world.configure(qstash_current_signing_key=None)
    person = await a_mentee(world.engine)

    assert (await deliver(world, person)).status_code == 401
    assert world.assistant.calls == []


@pytest.mark.parametrize(
    "payload",
    [{"user_id": "not-a-uuid"}, {}, {"user_id": 1}, {"user_id": str(uuid4()), "kind": "t12"}],
)
async def test_a_signed_body_that_is_not_a_draft_request_is_422(
    world: World, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload).encode()

    response = await world.client.post(PATH, content=body, headers=signed(body))

    assert response.status_code == 422
    assert world.assistant.calls == []


# --------------------------------------------------------------------------
# The callback drafts only into an empty bio
# --------------------------------------------------------------------------


async def test_an_empty_bio_is_drafted_and_saved_with_its_provenance(
    world: World, logs: pytest.LogCaptureFixture
) -> None:
    person = await a_mentee(world.engine)

    with logs.at_level(logging.INFO):
        response = await deliver(world, person)

    assert response.status_code == 200, response.text
    assert response.json() == {"drafted": True}
    bio = await bio_of(world.engine, person)
    assert bio["about_me"] == DRAFT
    assert bio["about_me_drafted_at"] is not None
    assert bio["about_me_prompt_version"] == "bio_draft-v1"
    lines = [r for r in logs.records if r.getMessage() == "mentee bio draft"]
    assert [r.outcome for r in lines] == ["drafted"]  # type: ignore[attr-defined]
    assert all(DRAFT not in str(vars(r)) for r in logs.records)


async def test_the_router_is_sent_a_mentee_draft_of_answers_only(world: World) -> None:
    person = await a_mentee(world.engine)

    await deliver(world, person)

    [call] = world.assistant.calls
    assert call.task == "bio_draft"
    assert call.source_data["role"] == "mentee"
    assert call.source_data["attempt"] == BACKGROUND_ATTEMPT
    assert call.source_data["field_of_study"] == "Computer Science"
    assert call.source_data["help_areas"]
    sent = json.dumps(call.source_data)
    for identity in (str(person.id), person.email, "Adaeze", "Okonkwo"):
        assert identity not in sent
    assert str(person.id) not in call.relation_id


async def test_a_background_draft_is_not_counted_against_the_person(world: World) -> None:
    person = await a_mentee(world.engine)

    await deliver(world, person)

    async with world.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM writing_assist_requests WHERE user_id = :u"),
                {"u": person.id},
            )
        ).scalar()
    assert rows == 0


async def test_a_whitespace_bio_counts_as_empty(world: World) -> None:
    person = await a_mentee(world.engine, about_me="   ")

    assert (await deliver(world, person)).json() == {"drafted": True}
    assert (await bio_of(world.engine, person))["about_me"] == DRAFT


async def test_a_written_bio_is_never_touched(world: World) -> None:
    person = await a_mentee(world.engine, about_me="My own words.")

    response = await deliver(world, person)

    assert response.json() == {"drafted": False}
    assert world.assistant.calls == []
    assert (await bio_of(world.engine, person))["about_me"] == "My own words."


async def test_a_bio_already_drafted_is_never_redrafted(world: World) -> None:
    """Provenance set over an empty text still means a draft happened."""
    person = await a_mentee(world.engine)
    await run_sql(
        world.engine,
        "UPDATE user_profiles SET about_me = '', about_me_drafted_at = now(), "
        "about_me_prompt_version = 'bio_draft-v1' WHERE user_id = :u",
        u=person.id,
    )

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_a_goal_gone_is_no_draft(world: World) -> None:
    person = await a_mentee(world.engine)
    await run_sql(world.engine, "DELETE FROM mentee_goals WHERE user_id = :u", u=person.id)

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_a_mentor_profile_since_is_no_draft(world: World) -> None:
    person = await a_mentee(world.engine)
    await run_sql(
        world.engine,
        "INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Mentor')",
        u=person.id,
    )

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_a_deleted_account_is_no_draft(world: World) -> None:
    person = await a_mentee(world.engine)
    await run_sql(world.engine, "UPDATE users SET deleted_at = now() WHERE id = :u", u=person.id)

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_an_unknown_user_is_no_draft(world: World) -> None:
    ghost = Person(uuid4(), "ghost@example.test", {})

    assert (await deliver(world, ghost)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_switched_off_since_queueing_is_no_draft(world: World) -> None:
    world.configure(mentee_bio_draft_enabled=False)
    person = await a_mentee(world.engine)

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_an_assistant_unconfigured_since_is_no_draft(world: World) -> None:
    world.assistant.available = False
    person = await a_mentee(world.engine)

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_a_person_typing_during_the_call_wins(world: World) -> None:
    person = await a_mentee(world.engine)
    loop = asyncio.get_running_loop()

    def type_a_bio() -> None:
        asyncio.run_coroutine_threadsafe(
            run_sql(
                world.engine,
                "UPDATE user_profiles SET about_me = 'Typed just now.' WHERE user_id = :u",
                u=person.id,
            ),
            loop,
        ).result(timeout=10)

    world.assistant.during = type_a_bio

    response = await deliver(world, person)

    assert response.status_code == 200
    assert response.json() == {"drafted": False}
    bio = await bio_of(world.engine, person)
    assert bio["about_me"] == "Typed just now."
    assert bio["about_me_drafted_at"] is None


async def test_a_mentor_profile_made_during_the_call_gets_no_bio(world: World) -> None:
    """The draft is owed only to someone with no mentor profile, re-checked by
    the save: a mentor's About is theirs, and frozen while in review."""
    person = await a_mentee(world.engine)
    loop = asyncio.get_running_loop()

    def become_a_mentor() -> None:
        asyncio.run_coroutine_threadsafe(
            run_sql(
                world.engine,
                "INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Mentor')",
                u=person.id,
            ),
            loop,
        ).result(timeout=10)

    world.assistant.during = become_a_mentor

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert (await bio_of(world.engine, person))["about_me"] is None


async def test_an_account_deleted_during_the_call_gets_no_bio(world: World) -> None:
    person = await a_mentee(world.engine)
    loop = asyncio.get_running_loop()

    def delete_the_account() -> None:
        asyncio.run_coroutine_threadsafe(
            run_sql(world.engine, "UPDATE users SET deleted_at = now() WHERE id = :u", u=person.id),
            loop,
        ).result(timeout=10)

    world.assistant.during = delete_the_account

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert (await bio_of(world.engine, person))["about_me"] is None


@pytest.mark.parametrize(
    "reply",
    [
        None,
        AssistResult(text="   ", prompt_version="bio_draft-v1"),
        AssistResult(text="Fine.", prompt_version="summary-v1"),
    ],
    ids=["no-answer", "empty-text", "unknown-version"],
)
async def test_no_usable_answer_leaves_the_bio_empty_and_stops_qstash(
    world: World, reply: AssistResult | None, logs: pytest.LogCaptureFixture
) -> None:
    """`None` is every no-result: review, budget, provider down, timeout, a
    config refusal. The client already retried what was transient."""
    world.assistant.reply = reply
    person = await a_mentee(world.engine)

    with logs.at_level(logging.INFO):
        response = await deliver(world, person)

    assert response.status_code == 200
    assert response.json() == {"drafted": False}
    assert (await bio_of(world.engine, person))["about_me"] is None
    lines = [r for r in logs.records if r.getMessage() == "mentee bio draft"]
    assert [r.outcome for r in lines] == ["no_answer"]  # type: ignore[attr-defined]


async def test_a_body_over_the_tasks_cap_is_not_sent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(TASKS, WritingAssistMode.DRAFT, Task("bio_draft", max_source_bytes=40))
    person = await a_mentee(world.engine)

    assert (await deliver(world, person)).json() == {"drafted": False}
    assert world.assistant.calls == []


async def test_a_redelivery_does_not_draft_twice(world: World) -> None:
    person = await a_mentee(world.engine)

    first, second = await deliver(world, person), await deliver(world, person)

    assert first.json() == {"drafted": True}
    assert second.json() == {"drafted": False}
    assert len(world.assistant.calls) == 1


async def test_a_redelivery_after_no_answer_resends_the_same_key(world: World) -> None:
    """So the router replays rather than spends twice."""
    world.assistant.reply = None
    person = await a_mentee(world.engine)

    await deliver(world, person)
    await deliver(world, person)

    first, second = world.assistant.calls
    assert first.idempotency_key == second.idempotency_key
    assert first.relation_id == second.relation_id


async def test_the_background_key_is_not_a_press_key(world: World) -> None:
    person = await a_mentee(world.engine)
    world.configure(writing_assist_enabled=True)
    world.assistant.reply = None

    await deliver(world, person)
    await world.client.post(
        "/api/v1/me/writing-assist",
        json={"field": "mentee_bio", "mode": "draft"},
        headers=person.headers,
    )

    background, press = world.assistant.calls
    assert background.idempotency_key != press.idempotency_key
