"""The signed, machine-only surface for recurring runtime work."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any

import httpx
import jwt
import pytest

from app.core.config import Settings
from app.infra.jobs.runner import JobResult
from app.main import create_app

BASE = "https://api.example.test"
# **At least 32 bytes, because PyJWT warns below that and warnings are errors.**
# HS256 keys shorter than the SHA-256 block raise `InsecureKeyLengthWarning`
# (RFC 7518 section 3.2). Padding the fixture is the fix; silencing the warning
# would hide it everywhere, including where it would be telling the truth.
KEY = "current-test-signing-key-at-least-32-bytes"
NEXT_KEY = "next-test-signing-key-at-least-32-bytes"


class RecordingJobs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, name: str, **kwargs: Any) -> JobResult:
        self.calls.append({"name": name, **kwargs})
        return JobResult(name=name, job_id=kwargs.get("job_id"), status="completed")


def signature(
    body: bytes,
    path: str,
    *,
    key: str = KEY,
    issuer: str = "Upstash",
    expires: dt.timedelta = dt.timedelta(minutes=5),
    not_before: dt.timedelta = dt.timedelta(),
) -> str:
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "sub": f"{BASE}{path}",
            "exp": int((now + expires).timestamp()),
            "nbf": int((now + not_before).timestamp()),
            "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode(),
        },
        key,
        algorithm="HS256",
    )


@pytest.fixture
def job_app() -> tuple[Any, RecordingJobs]:
    app = create_app(
        Settings(
            _env_file=None,
            environment="staging",
            public_base_url=BASE,
            qstash_current_signing_key=KEY,
            qstash_next_signing_key=NEXT_KEY,
        )
    )
    jobs = RecordingJobs()
    app.state.runtime_jobs = jobs
    return app, jobs


async def post(
    app: Any,
    name: str,
    body: bytes,
    *,
    token: str | None = None,
) -> httpx.Response:
    path = f"/api/v1/internal/jobs/{name}"
    headers = {"Content-Type": "application/json", "Upstash-Message-Id": "msg-42"}
    if token is not None:
        headers["Upstash-Signature"] = token
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, content=body, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "settle-sessions",
        "credit-reminders",
        "monthly-credits",
        "expire-credits",
        "sync-institutions",
    ],
)
async def test_all_five_signed_endpoints_call_the_shared_runner(
    job_app: tuple[Any, RecordingJobs], name: str
) -> None:
    app, jobs = job_app
    path = f"/api/v1/internal/jobs/{name}"
    job_id = f"edufurther-staging-{name}"
    body = json.dumps({"job_id": job_id}).encode()

    response = await post(app, name, body, token=signature(body, path))

    assert response.status_code == 200, response.text
    assert jobs.calls[-1] == {
        "name": name,
        "job_id": job_id,
        "message_id": "msg-42",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "expired", "future", "issuer", "body", "path"])
async def test_untrusted_deliveries_are_problem_details(
    job_app: tuple[Any, RecordingJobs], failure: str
) -> None:
    app, jobs = job_app
    name = "settle-sessions"
    path = f"/api/v1/internal/jobs/{name}"
    body = json.dumps({"job_id": "edufurther-staging-settle-sessions"}).encode()
    token: str | None
    if failure == "missing":
        token = None
    elif failure == "expired":
        token = signature(body, path, expires=dt.timedelta(minutes=-1))
    elif failure == "future":
        token = signature(body, path, not_before=dt.timedelta(minutes=5))
    elif failure == "issuer":
        token = signature(body, path, issuer="SomebodyElse")
    elif failure == "body":
        token = signature(b"{}", path)
    else:
        token = signature(body, "/api/v1/internal/jobs/credit-reminders")

    response = await post(app, name, body, token=token)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert jobs.calls == []


@pytest.mark.asyncio
async def test_next_signing_key_is_accepted_during_rotation(
    job_app: tuple[Any, RecordingJobs],
) -> None:
    app, jobs = job_app
    name = "settle-sessions"
    path = f"/api/v1/internal/jobs/{name}"
    body = json.dumps({"job_id": "edufurther-staging-settle-sessions"}).encode()

    response = await post(app, name, body, token=signature(body, path, key=NEXT_KEY))

    assert response.status_code == 200
    assert len(jobs.calls) == 1


@pytest.mark.asyncio
async def test_a_job_id_for_another_environment_is_refused(
    job_app: tuple[Any, RecordingJobs],
) -> None:
    app, jobs = job_app
    name = "settle-sessions"
    path = f"/api/v1/internal/jobs/{name}"
    body = json.dumps({"job_id": "edufurther-production-settle-sessions"}).encode()

    response = await post(app, name, body, token=signature(body, path))

    assert response.status_code == 422
    assert jobs.calls == []
