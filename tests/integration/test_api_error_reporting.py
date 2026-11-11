"""An unhandled error on an authenticated route reaches Sentry and PostHog once
each, carrying the member's id only when their analytics consent is accepted,
read by `CURRENT_USER` rather than on the error path (decision 339)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sentry_sdk
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import token_for
from tests.posthog_fakes import recording_client

from app.api.deps.core import CurrentUserDep
from app.core.config import Settings
from app.infra.clients.posthog import PostHogSink
from app.infra.clients.posthog_errors import PostHogErrorReporter
from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.observability import configure_sentry
from conftest import Recording, add_user, bearer, build_api_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

HOST = "https://ph.example.test"
TOKEN = "phc_public"  # noqa: S105 - a public ingestion token, made up


async def a_member(
    engine: AsyncEngine, consent: bool | None, *, internal: bool = False
) -> tuple[UUID, dict[str, str]]:
    """A member with ``consent``; ``internal`` gives a demo address, which the
    database marks `is_internal` (decision 329)."""
    domain = DEMO_DOMAIN if internal else "example.test"
    async with engine.begin() as conn:
        user = await add_user(conn, f"{uuid4()}@{domain}")
        if consent is not None:
            await conn.execute(
                text(
                    "INSERT INTO analytics_consents (user_id, granted, version) "
                    "VALUES (:u, :g, 'v1')"
                ),
                {"u": user, "g": consent},
            )
    return user, bearer(await token_for(engine, user))


@pytest.mark.parametrize(
    ("consent", "internal", "identified"),
    [(None, False, False), (False, False, False), (True, True, False), (True, False, True)],
)
async def test_a_500_is_reported_to_both_with_consent_driven_identity(
    db_engine: AsyncEngine,
    recording: list[dict[str, Any]],
    consent: bool | None,
    internal: bool,
    identified: bool,
) -> None:
    configure_sentry(
        Settings(_env_file=None, sentry_dsn=SecretStr("https://public@o0.ingest.sentry.io/1")),
        transport=Recording,
    )
    app = build_api_app(db_engine, None, Settings(_env_file=None))
    seen: list[httpx.Request] = []
    sink = PostHogSink(host=HOST, project_token=TOKEN, client=recording_client(seen))
    reporter = PostHogErrorReporter(sink, release="abc123", environment="ci")
    app.state.error_reporter = reporter

    @app.get("/api/v1/broken/{thing_id}")
    async def broken(thing_id: str, user: CurrentUserDep) -> None:
        del thing_id, user
        raise RuntimeError("the database fell over")

    user, token = await a_member(db_engine, consent, internal=internal)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/broken/{uuid4()}", headers=token)
    await reporter.drain()
    sentry_sdk.flush()

    assert response.status_code == 500
    assert response.json()["title"] == "Internal Server Error"
    (request,) = seen
    (event,) = json.loads(request.content)["batch"]
    assert event["properties"]["route"] == "/api/v1/broken/{thing_id}"
    (sentry_event,) = recording
    assert sentry_event["tags"]["route"] == "/api/v1/broken/{thing_id}"
    if identified:
        assert event["distinct_id"] == str(user)
        assert sentry_event["user"] == {"id": str(user)}
    else:
        assert event["distinct_id"] != str(user)
        assert event["properties"]["$process_person_profile"] is False
        assert "user" not in sentry_event
        assert str(user) not in json.dumps(sentry_event)
