"""The repository policy for recurring application work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import QSTASH_EU, Settings
from app.infra.jobs.manifest import (
    ManifestError,
    load_manifest,
    resolve_manifest,
    schedule_id,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "config" / "runtime-schedules.json"


def test_the_manifest_declares_the_five_runtime_jobs_in_utc() -> None:
    manifest = load_manifest(MANIFEST)

    assert manifest.timezone == "UTC"
    assert {job.name for job in manifest.jobs} == {
        "settle-sessions",
        "credit-reminders",
        "monthly-credits",
        "expire-credits",
        "sync-institutions",
    }
    assert {job.name: job.cron for job in manifest.jobs} == {
        "settle-sessions": "30 * * * *",
        "credit-reminders": "0 7 * * *",
        "monthly-credits": "0 6 1 * *",
        "expire-credits": "0 5 1 * *",
        "sync-institutions": "0 4 * * 1",
    }


@pytest.mark.parametrize("environment", ["local", "ci", "production"])
def test_local_ci_and_production_resolve_disabled(environment: str) -> None:
    resolved = resolve_manifest(
        load_manifest(MANIFEST),
        environment=environment,
        public_base_url="https://api.example.test",
        overrides={},
    )

    assert resolved
    assert all(not schedule.enabled for schedule in resolved)


def test_staging_resolves_enabled_with_stable_environment_ids() -> None:
    resolved = resolve_manifest(
        load_manifest(MANIFEST),
        environment="staging",
        public_base_url="https://api.example.test/",
        overrides={},
    )

    assert all(schedule.enabled for schedule in resolved)
    assert [schedule.id for schedule in resolved] == sorted(schedule.id for schedule in resolved)
    assert resolved[0].destination.startswith("https://api.example.test/api/v1/internal/jobs/")
    assert schedule_id("staging", "settle-sessions") == "edufurther-staging-settle-sessions"


def test_only_cron_and_enabled_may_be_overridden() -> None:
    manifest = load_manifest(MANIFEST)
    resolved = resolve_manifest(
        manifest,
        environment="staging",
        public_base_url="https://api.example.test",
        overrides={"settle-sessions": {"cron": "15 * * * *", "enabled": False}},
    )
    settle = next(item for item in resolved if item.name == "settle-sessions")

    assert settle.cron == "15 * * * *"
    assert settle.enabled is False

    with pytest.raises(ManifestError, match="unknown override key"):
        resolve_manifest(
            manifest,
            environment="staging",
            public_base_url="https://api.example.test",
            overrides={"settle-sessions": {"timeout": "1s"}},
        )


def test_unknown_jobs_and_incorrect_override_types_are_refused() -> None:
    manifest = load_manifest(MANIFEST)
    with pytest.raises(ManifestError, match="unknown job"):
        resolve_manifest(
            manifest,
            environment="staging",
            public_base_url="https://api.example.test",
            overrides={"invented": {"enabled": True}},
        )
    with pytest.raises(ManifestError, match="enabled"):
        resolve_manifest(
            manifest,
            environment="staging",
            public_base_url="https://api.example.test",
            overrides={"settle-sessions": {"enabled": "yes"}},
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"version": 2}, "version"),
        ({"timezone": "America/New_York"}, "UTC"),
    ],
)
def test_unsupported_versions_and_non_utc_manifests_are_refused(
    tmp_path: Path, mutation: dict[str, object], message: str
) -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8")) | mutation
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises((ManifestError, ValidationError), match=message):
        load_manifest(path)


def test_duplicate_job_names_are_refused(tmp_path: Path) -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["jobs"].append(raw["jobs"][0])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError, match="duplicate"):
        load_manifest(path)


def test_schedule_overrides_are_one_strict_json_object() -> None:
    settings = Settings(
        _env_file=None,
        qstash_schedule_overrides='{"settle-sessions":{"enabled":false}}',
    )
    assert settings.qstash_schedule_overrides == {"settle-sessions": {"enabled": False}}

    with pytest.raises(ValidationError):
        Settings(_env_file=None, qstash_schedule_overrides='{"settle-sessions":[]}')


def test_a_blank_qstash_url_falls_back_to_the_default_region() -> None:
    """**Blank is how "unset" arrives**, and taking it literally breaks the EU
    deployments this setting exists to leave alone.

    A GitHub Actions `${{ vars.QSTASH_URL }}` renders as an empty string when the
    variable does not exist, and a `.env` line left as `QSTASH_URL=` is the same.
    Read literally, both build a *relative* URL — `/v2/publish` — which fails at
    request time with a host error naming nothing.
    """
    for blank in ("", "   "):
        assert Settings(_env_file=None, qstash_url=blank).qstash_url == QSTASH_EU


def test_a_pasted_console_url_keeps_no_trailing_slash() -> None:
    """A doubled slash is a `404`, indistinguishable from the wrong-region `404`."""
    settings = Settings(_env_file=None, qstash_url="https://qstash-us-east-1.upstash.io/")

    assert settings.qstash_url == "https://qstash-us-east-1.upstash.io"


def test_a_qstash_url_without_a_scheme_is_refused_at_startup() -> None:
    """**The silent failure this field exists to prevent.**

    `qstash-us-east-1.upstash.io` is what a console shows and looks entirely
    right. Without a scheme it builds a *relative* URL, so every publish raises
    `UnsupportedProtocol` — and `session_writer` catches `SchedulerError` and
    logs it at INFO, so reminders stop being scheduled and nothing says so.

    Refusing at startup turns a silent production degradation into a boot error.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qstash_url="qstash-us-east-1.upstash.io")
