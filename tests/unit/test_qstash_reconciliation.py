"""A deterministic, environment-scoped QStash schedule diff."""

from __future__ import annotations

from dataclasses import replace

import httpx

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
        base_url="https://qstash.upstash.io/v2",
        transport=httpx.MockTransport(handle),
    )
    schedule = desired()

    QStashSchedules("secret", client).put(schedule)

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
        base_url="https://qstash.upstash.io/v2",
        transport=httpx.MockTransport(handle),
    )

    (listed,) = QStashSchedules("secret", client).list()

    assert listed.body == desired().canonical_body
