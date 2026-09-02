"""Show or apply repository schedule policy to one QStash environment."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.infra.jobs.manifest import load_manifest, resolve_manifest
from app.infra.jobs.reconcile import QStashSchedules, apply_reconciliation, plan_reconciliation

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "runtime-schedules.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=("staging", "production"))
    parser.add_argument(
        "--apply", action="store_true", help="Apply the printed diff; default is dry-run."
    )
    args = parser.parse_args()
    settings = get_settings()
    if settings.environment != args.environment:
        raise ConfigurationError(
            "EDUFURTHER_ENVIRONMENT must match the selected reconciliation environment"
        )
    if not settings.public_base_url:
        raise ConfigurationError("PUBLIC_BASE_URL is required to reconcile schedules")
    token = settings.qstash_token
    if token is None:
        raise ConfigurationError("QSTASH_TOKEN is required to inspect schedules")

    resolved = resolve_manifest(
        load_manifest(MANIFEST),
        environment=args.environment,
        public_base_url=settings.public_base_url,
        overrides=settings.qstash_schedule_overrides,
    )
    print("Resolved configuration:")
    print(json.dumps([asdict(schedule) for schedule in resolved], indent=2, sort_keys=True))

    client = QStashSchedules(token.get_secret_value(), settings.qstash_url)
    changes = plan_reconciliation(resolved, client.list(), environment=args.environment)
    print("Diff:")
    if not changes:
        print("  no changes")
    for change in changes:
        print(f"  {change.action}: {change.schedule.id}")
    if args.apply:
        apply_reconciliation(client, changes)
        print(f"Applied {len(changes)} change(s).")
    else:
        print("Dry run; QStash was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
