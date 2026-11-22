"""Transform a legacy snapshot into the profile tables, then reconcile.

    # transform and report, touching no database
    uv run python scripts/load_profiles.py --from-export script-data-dev --dry-run

    # load, then reconcile
    uv run python scripts/load_profiles.py --from-export script-data-dev

    # on a database people already use: add only the education read from its
    # Creator, insert-only (decision 364)
    uv run python scripts/load_profiles.py --from-export <dir> --education-by-creator

**Run this after ``load_identity.py``.** Every row here hangs off a user, and the
mentor and mentee junctions hang off ``mentor_profiles`` and ``mentee_goals`` in
turn. Out of order it fails on a foreign key, which is the right failure and a
confusing one to read at four in the morning.

A dry run is the pre-load check the runbook asks for, not a rehearsal of the
write: unmapped values, rows no user claims, and award years outside the column's
CHECK are all found *before* anything is touched.

``institution_id`` is left null on every education row. Matching schools against
hipolabs is a separate, re-runnable pass — the link is an UPDATE against these
rows, so it can be retuned without reloading them (ADR 0008).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.bubble import EXPORT_TIMEZONE
from app.domain.resolve import COUNTRY_ALIASES, resolve_names
from app.domain.transform.profiles import ProfilePlan, plan_profiles
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNRESOLVED,
    configure_streams,
    open_export,
    report_unresolved,
)
from app.infra.etl.profiles import ProfileLoader, lookup_maps
from app.infra.etl.reconcile import reconcile_profiles
from app.infra.etl.satellites import reference_maps, user_id_map

#: The tables this loader writes, in the order they must be written.
TABLES = (
    "mentor_profiles",
    "mentee_goals",
    "mentor_service_offerings",
    "mentee_goal_countries",
    "mentee_goal_needs",
    "education_entries",
    "user_awards",
)


def describe(plan: ProfilePlan) -> None:
    """Counts and identifiers only — never a field value.

    A diagnostic over member data must not put a name, an email or a bio into a
    terminal or a CI log.
    """
    print(f"mentor profiles {len(plan.mentors)}")
    print(f"education       {len(plan.education)}")
    print(f"education by creator {len(plan.education_by_creator)}")
    print(f"mentee goals    {len(plan.goals)}")
    print(f"awards          {len(plan.awards)}")

    for thing, ids in sorted(plan.unattached.items()):
        # Named, not counted. An unattached row is either legacy debris or a
        # broken link, and only the id distinguishes them.
        print(f"unattached {thing}: {len(ids)} — {', '.join(ids)}")
    if plan.schoolless_education:
        ids = plan.schoolless_education
        print(f"skipped education, no school: {len(ids)} — {', '.join(ids)}")
    for mismatch in plan.creator_mismatches:
        print(f"   CREATOR MISMATCH {mismatch}")
    for rejected in plan.rejected_award_years:
        print(f"   year out of range, loading null: {rejected}")
    for rejected in plan.rejected_education_dates:
        print(f"   dates out of order, loading empty: {rejected}")
    for error in plan.errors:
        print(f"   refused  {error}")


async def load(plan: ProfilePlan) -> int:
    """Load, reconcile, and report. Returns 0 clean, 2 if a name went unresolved."""
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            users = await user_id_map(connection)
            country_reference, _ = await reference_maps(connection)
            countries = resolve_names(plan.country_names(), country_reference, COUNTRY_ALIASES)
            offerings, degrees = await lookup_maps(connection)

            refuse_missing_slugs(
                services=plan.service_slugs(),
                degrees=plan.degree_slugs(),
                offerings=offerings,
                degree_ids=degrees,
            )

            # Every table, for the whole load. `trg_set_updated_at` is
            # unconditional, so without this the importer stamps its own clock
            # over every migrated timestamp — silently, with row counts and
            # null-rate checks all still passing.
            async with timestamps_from_source_across(connection, TABLES):
                counts = await ProfileLoader(connection).load(
                    users=users,
                    mentors=plan.mentors,
                    education=plan.education,
                    awards=plan.awards,
                    goals=plan.goals,
                    offerings=offerings,
                    degrees=degrees,
                    countries=countries,
                )

            print(
                f"\nloaded {counts.mentor_profiles} mentor profiles, "
                f"{counts.mentor_services} offerings, {counts.education} education entries, "
                f"{counts.awards} awards, {counts.goals} goals, "
                f"{counts.goal_countries} goal countries, {counts.goal_needs} goal needs"
            )
            if counts.empty_tables:
                # Said out loud. A dev snapshot legitimately writes no goal
                # countries — `Country Goal` is blank on every row — and a report
                # that leaves that to be inferred from a zero reads as coverage.
                print(f"wrote no rows to: {', '.join(counts.empty_tables)}")

            unresolved = report_unresolved("countries", counts.countries_skipped)

            result = await reconcile_profiles(connection, plan, counts)
            print()
            print(result.report())
            if not result.ok:
                # Raised inside the transaction, so a failed reconciliation rolls
                # the load back. Reconciling after a commit could only report a
                # problem it was no longer able to undo.
                raise RuntimeError("reconciliation failed; rolling back")
    finally:
        await engine.dispose()

    if unresolved:
        print(
            "\nloaded, but names above resolved to nothing — add an alias or widen the seed",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED
    return EXIT_OK


def refuse_missing_slugs(
    *,
    services: set[str],
    degrees: set[str],
    offerings: Mapping[str, object],
    degree_ids: Mapping[str, object],
) -> None:
    """A slug the transform emits and the database does not hold means the seed
    and the mapping have drifted. Raised before any write: the alternative is a
    foreign-key error partway through, or a degree silently written as null."""
    missing = sorted(services - set(offerings)) + sorted(degrees - set(degree_ids))
    if missing:
        raise RuntimeError(f"lookup slugs missing from the database: {missing}")


async def add_education_by_creator(plan: ProfilePlan) -> int:
    """Insert-only: no other table, and no row already there, is touched."""
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    try:
        async with engine.begin() as connection:
            _, degrees = await lookup_maps(connection)
            chosen = plan.education_read_from_creator()
            refuse_missing_slugs(
                services=set(),
                degrees={row.degree_level_slug for row in chosen if row.degree_level_slug},
                offerings={},
                degree_ids=degrees,
            )
            async with timestamps_from_source_across(connection, ("education_entries",)):
                added = await ProfileLoader(connection).add_education(
                    users=await user_id_map(connection),
                    education=chosen,
                    degrees=degrees,
                )
    finally:
        await engine.dispose()
    print(
        f"\nadded {added.added} education entries; already there {added.already_there}; "
        f"skipped, has their own education {added.has_own_education}; "
        f"skipped, account deleted {added.deleted_account}"
    )
    return EXIT_OK


async def run(args: argparse.Namespace) -> int:
    source = open_export(args.from_export)
    plan = plan_profiles(
        list(source.read(args.users)),
        education_records=list(source.read(args.education)),
        goal_records=list(source.read(args.goals)),
        service_records=list(source.read(args.services)),
        mentor_records=list(source.read(args.mentors)),
        award_records=list(source.read(args.awards)),
        export_timezone=EXPORT_TIMEZONE,
        this_year=datetime.now(UTC).year,
    )
    describe(plan)

    if not plan.ok:
        print("\nrefusing to load — resolve the above first", file=sys.stderr)
        return EXIT_REFUSED

    if args.dry_run:
        print("\ndry run — nothing written")
        return EXIT_OK

    if args.education_by_creator:
        return await add_education_by_creator(plan)
    return await load(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-export", type=Path, required=True)
    parser.add_argument("--users", default="allusers", help="the user Thing")
    parser.add_argument("--education", default="education")
    parser.add_argument("--goals", default="menteegoal")
    parser.add_argument("--services", default="mentorservice")
    parser.add_argument("--mentors", default="mentorsearch")
    parser.add_argument("--awards", default="scholarshipaward")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--education-by-creator",
        action="store_true",
        help="insert only the education read from its Creator (decision 364)",
    )
    args = parser.parse_args()

    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
