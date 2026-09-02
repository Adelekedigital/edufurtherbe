"""A deterministic, environment-scoped QStash schedule diff."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app.core.config import QSTASH_EU
from app.core.errors import UpstreamError
from app.infra.jobs.manifest import ResolvedSchedule
from app.infra.jobs.reconcile import (
    ExistingSchedule,
    QStashSchedules,
    plan_reconciliation,
    policy_fingerprint,
)


def desired(name: str = "settle-sessions") -> ResolvedSchedule:
    return ResolvedSchedule(
        id=f"edufurther-staging-{name}",
        name=name,
        destination=f"https://api.example.test/api/v1/internal/jobs/{name}",
        cron="30 * * * *",
        enabled=True,
        timeout="120s",
        retries=3,
        body={"job_id": f"edufurther-staging-{name}"},
    )


def existing(item: ResolvedSchedule, *, labels: tuple[str, ...] | None = None) -> ExistingSchedule:
    return ExistingSchedule(
        id=item.id,
        destination=item.destination,
        cron=item.cron,
        method="POST",
        body=item.canonical_body,
        retries=item.retries,
        paused=False,
        labels=labels or (item.scope_label, item.fingerprint_label),
    )


def test_a_second_reconciliation_has_zero_diff() -> None:
    item = desired()
    assert plan_reconciliation([item], [existing(item)], environment="staging") == ()


def test_delivery_policy_changes_force_an_update_even_when_list_omits_timeout() -> None:
    before = desired()
    after = replace(before, timeout="30s")

    (change,) = plan_reconciliation([after], [existing(before)], environment="staging")

    assert change.action == "update"
    assert change.schedule.id == after.id
    assert policy_fingerprint(after) != policy_fingerprint(before)


def test_changes_are_sorted_and_only_the_selected_environment_is_managed() -> None:
    wanted = [desired("sync-institutions"), desired("credit-reminders")]
    stale = desired("obsolete")
    other = replace(stale, id="edufurther-production-obsolete")

    changes = plan_reconciliation(
        wanted,
        [existing(stale), existing(other)],
        environment="staging",
    )

    assert [(change.action, change.schedule.id) for change in changes] == [
        ("create", "edufurther-staging-credit-reminders"),
        ("delete", "edufurther-staging-obsolete"),
        ("create", "edufurther-staging-sync-institutions"),
    ]


def test_a_disabled_schedule_is_removed_if_it_exists() -> None:
    enabled = desired()
    disabled = replace(enabled, enabled=False)

    (change,) = plan_reconciliation([disabled], [existing(enabled)], environment="staging")

    assert change.action == "delete"


def test_an_unrelated_qstash_schedule_is_never_deleted() -> None:
    unrelated = ExistingSchedule(
        id="another-product-nightly",
        destination="https://elsewhere.test/job",
        cron="0 0 * * *",
        method="POST",
        body="{}",
        retries=3,
        paused=False,
        labels=(),
    )

    assert plan_reconciliation([], [unrelated], environment="staging") == ()


def test_create_sets_every_delivery_policy_header_explicitly() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"scheduleId": desired().id})

    client = httpx.Client(
        base_url=f"{QSTASH_EU}/v2",
        transport=httpx.MockTransport(handle),
    )
    schedule = desired()

    QStashSchedules("secret", QSTASH_EU, client).put(schedule)

    (request,) = seen
    assert request.method == "POST"
    assert request.headers["Upstash-Schedule-Id"] == schedule.id
    assert request.headers["Upstash-Method"] == "POST"
    assert request.headers["Upstash-Retries"] == "3"
    assert request.headers["Upstash-Timeout"] == "120s"
    assert request.headers["Content-Type"] == "application/json"
    assert request.content.decode() == schedule.canonical_body
    assert schedule.fingerprint_label in request.headers["Upstash-Label"]


def test_list_is_sorted_and_accepts_base64_encoded_bodies() -> None:
    encoded = "eyJqb2JfaWQiOiJlZHVmdXJ0aGVyLXN0YWdpbmctc2V0dGxlLXNlc3Npb25zIn0="

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "scheduleId": "edufurther-staging-settle-sessions",
                    "destination": desired().destination,
                    "cron": desired().cron,
                    "method": "POST",
                    "body": encoded,
                    "retries": 3,
                    "labels": [desired().scope_label, desired().fingerprint_label],
                }
            ],
        )

    client = httpx.Client(
        base_url=f"{QSTASH_EU}/v2",
        transport=httpx.MockTransport(handle),
    )

    (listed,) = QStashSchedules("secret", QSTASH_EU, client).list()

    assert listed.body == desired().canonical_body


def test_the_reconciler_talks_to_the_configured_region() -> None:
    """**The symmetric half of the scheduler's regional test.**

    Every other case here injects its own `httpx.Client`, so the origin argument
    goes unused and could be ignored entirely without a single test noticing.
    This is the one that builds the client the way production does.

    A trailing slash is trimmed for the same reason it is in the publisher: a
    pasted console URL carries one, and `https://host//v2/schedules` is a `404`
    wearing the same face as the wrong-region `404`.
    """
    # `httpx` normalises a `base_url` to end in a slash, which is what makes
    # `/v2/` + `schedules` resolve; the assertion matches that rather than
    # pretending otherwise.
    expected = "https://qstash-us-east-1.upstash.io/v2/"

    with_slash = QStashSchedules("t", "https://qstash-us-east-1.upstash.io/")
    without = QStashSchedules("t", "https://qstash-us-east-1.upstash.io")

    assert str(without.client.base_url) == expected
    assert str(with_slash.client.base_url) == expected


def test_a_wrong_region_explains_itself_in_the_error() -> None:
    """**The message that cost an afternoon.**

    QStash answers a wrong region with `404` and names the region in the body.
    `httpx` renders only the status line, so the operator saw "404 Not Found"
    against a URL spelled exactly right and went looking for a broken path — the
    configuration being wrong is the last thing that comes to mind.

    The body is the answer, so it has to travel with the error.
    """
    body = (
        '{"error":"user (abc) not found in this region (eu-central-1). '
        'Check that you are using the correct endpoint."}'
    )
    client = httpx.Client(
        base_url=f"{QSTASH_EU}/v2",
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, content=body)),
    )

    with pytest.raises(UpstreamError) as raised:
        QStashSchedules("t", QSTASH_EU, client).list()

    assert "not found in this region" in str(raised.value)
    assert "eu-central-1" in str(raised.value)
