"""Transform a legacy snapshot into the identity tables, then reconcile.

    # transform and report, touching no database
    uv run python scripts/load_identity.py --from-export script-data-dev \
        --thing allusers --profiles userprofile --dry-run

    # load, then reconcile
    uv run python scripts/load_identity.py --from-export script-data-dev \
        --thing allusers --profiles userprofile

A dry run is not a rehearsal of the write — it is the pre-load check the runbook
asks for. Duplicate emails, unmappable values and orphaned profiles are found
*before* anything is touched, rather than by a constraint halfway through a load
with rows already written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.resolve import COUNTRY_ALIASES, LANGUAGE_ALIASES, resolve_names
from app.domain.transform import IdentityPlan, plan_identity
from app.infra.clients.bubble import JsonExportSource
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.loader import UserLoader
from app.infra.etl.reconcile import reconcile_users
from app.infra.etl.satellites import SatelliteLoader, reference_maps, user_id_map

# Duplicated rather than imported from `scripts/extract_bubble.py`: `scripts/` is
# not a package, so a cross-script import resolves only when the repository root
# happens to be on sys.path — true under pytest, false under
# `uv run python scripts/...`. A test asserts the two stay equal.
EXPORT_TIMEZONE = ZoneInfo("America/New_York")


def describe(plan: IdentityPlan) -> None:
    """Counts and identifiers only — never a field value.

    A diagnostic over member data must not put names or emails into a terminal or
    a CI log. Duplicate emails are the one exception, because an operator cannot
    resolve one without knowing which address collided.
    """
    print(f"users        {len(plan.users)}")
    print(f"profiles     {len(plan.profiles)}")
    print(f"onboarding   {len(plan.onboarding)}")
    print(f"admin grants {len(plan.admin_grants)}")
    print(f"identities   {len(plan.identities)}")

    if not plan.source_carries_identities:
        # Zero from an export means "wrong source"; zero from an API extract
        # means "nobody linked a provider". A bare count conflates the two, and
        # only one of them is a problem.
        print("             ^ this source carries no provider ids; use an API extract")

    if plan.orphaned_profiles:
        print(f"orphaned profiles: {len(plan.orphaned_profiles)}")
    for error in plan.errors:
        print(f"   refused  {error}")
    for email, ids in plan.duplicate_emails.items():
        print(f"   DUPLICATE {email}: {', '.join(ids)}")


async def load(plan: IdentityPlan) -> int:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            users_written = await UserLoader(connection).load(plan.users)
            ids = await user_id_map(connection)

            country_reference, language_reference = await reference_maps(connection)
            countries = resolve_names(plan.country_names(), country_reference, COUNTRY_ALIASES)
            languages = resolve_names(plan.language_names(), language_reference, LANGUAGE_ALIASES)

            counts = await SatelliteLoader(connection).load(
                users=ids,
                profiles=plan.profiles,
                onboarding=plan.onboarding,
                identities=plan.identities,
                admin_grants=plan.admin_grants,
                countries=countries,
                languages=languages,
            )

            print(
                f"\nloaded {users_written} users, {counts.profiles} profiles, "
                f"{counts.languages} languages, {counts.onboarding} onboarding, "
                f"{counts.identities} identities, {counts.admin_grants} admin grants"
            )
            for label, skipped in (
                ("countries", counts.countries_skipped),
                ("languages", counts.languages_skipped),
            ):
                if skipped:
                    # By name, not by count: the next action is always to decide
                    # an alias or widen a seed, and a number supports neither.
                    print(f"UNRESOLVED {label}: {', '.join(skipped)}")

            result = await reconcile_users(connection, plan.users)
            print(result.report())
            if not result.ok:
                # Raised inside the transaction, so a failed reconciliation rolls
                # the load back. Reconciling after a commit could only report a
                # problem it was no longer able to undo.
                raise RuntimeError("reconciliation failed; rolling back")
    finally:
        await engine.dispose()

    return 0


async def run(args: argparse.Namespace) -> int:
    source = JsonExportSource(args.from_export, timezone=EXPORT_TIMEZONE)
    plan = plan_identity(
        list(source.read(args.thing)),
        list(source.read(args.profiles)),
        export_timezone=EXPORT_TIMEZONE,
    )
    describe(plan)

    if not plan.ok:
        print("\nrefusing to load — resolve the above first", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    return await load(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-export", type=Path, required=True)
    parser.add_argument("--thing", required=True, help="the user Thing")
    parser.add_argument("--profiles", default="userprofile", help="the PersonalInfo Thing")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Bubble field names contain emoji, and a Windows console defaults to cp1252.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
