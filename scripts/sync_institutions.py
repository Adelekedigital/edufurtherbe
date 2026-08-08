"""Mirror the university catalogue, then link education entries to it.

    # fetch and report, touching no database
    uv run python scripts/sync_institutions.py --dry-run

    # mirror and link
    uv run python scripts/sync_institutions.py

    # from a snapshot somebody already took, instead of over the network
    uv run python scripts/sync_institutions.py --from-file catalogue.json

**One implementation, two triggers.** A weekly GitHub Actions workflow runs this
same script; there is no separate scheduled path, because two implementations of
one job drift and the one nobody runs by hand is the one that rots.

The catalogue is fetched over **HTTPS from the source repository**. The
`universities.hipolabs.com` API is never called: it serves plain HTTP, so a
browser cannot reach it from an HTTPS page at all, and a server reaching it would
be making an unencrypted request on a user's behalf (ADR 0020).

Safe to re-run. The mirror upserts on `domain`, and the link only fills an
`institution_id` that is still null.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.domain.institutions import CatalogueError, CatalogueRow, to_catalogue_row
from app.infra.clients.hipolabs import Catalogue, FileCatalogue, HipolabsCatalogue
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, EXIT_UNRESOLVED, configure_streams
from app.infra.etl.institutions import country_ids, link_education, mirror

#: Only `institutions` is written by the mirror; `education_entries` is written
#: by the link pass and carries no source timestamp to protect, but is held
#: anyway so a re-link cannot restamp a migrated row.
TABLES = ("institutions", "education_entries")

FETCH_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def read_catalogue(args: argparse.Namespace) -> Catalogue:
    if args.from_file:
        return FileCatalogue(args.from_file).fetch()
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        return HipolabsCatalogue(client).fetch()


def describe(catalogue: Catalogue, rows: list[CatalogueRow], refused: list[str]) -> None:
    print(f"catalogue      {len(catalogue.records)} records")
    print(f"commit         {catalogue.source_commit or '(unknown)'}")
    print(f"usable rows    {len(rows)}")
    if refused:
        # Named, not counted. A refused record is a change in the source's shape,
        # which is a different problem from a country we cannot resolve.
        print(f"refused        {len(refused)}")
        for detail in refused[:10]:
            print(f"   {detail}")


async def run_sync(rows: list[CatalogueRow]) -> int:
    engine = create_async_engine(resolve_async_dsn(get_settings()))
    synced_at = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            countries = await country_ids(connection)

            # The statement sets `updated_at` itself, so an unchanged row keeps
            # the timestamp it had while still being stamped as seen.
            async with timestamps_from_source_across(connection, TABLES):
                counts = await mirror(connection, rows, countries, synced_at=synced_at)
                links = await link_education(connection)

            print(
                f"\nmirrored {counts.stored} institutions "
                f"({counts.written} written, {len(counts.collapsed_domains)} domains collapsed)"
            )
            if counts.collapsed_domains:
                # Two upstream names, one domain — one institution. Correct, and
                # said out loud so the row count is not read as a loss.
                print(f"   collapsed: {', '.join(counts.collapsed_domains)}")
            if counts.skipped_no_country:
                print(
                    f"   skipped {counts.skipped_no_country} with an unresolvable country: "
                    f"{', '.join(counts.unresolved_codes)}"
                )
            print(f"linked {links.linked}/{links.considered} education entries")
            if links.unmatched:
                print(f"   unmatched ({len(links.unmatched)}), still showing their raw name:")
                for name in links.unmatched[:20]:
                    print(f"      {name}")
            if links.ambiguous:
                # Separate from unmatched on purpose: this needs a person to say
                # *which* institution, not a person to add a missing one.
                print(f"   ambiguous ({len(links.ambiguous)}), the catalogue holds each twice:")
                for name in links.ambiguous[:20]:
                    print(f"      {name}")
    finally:
        await engine.dispose()

    # Unmatched and ambiguous entries are the policy working, not a failure —
    # the raw name still renders. But exit 0 is the one signal that says nothing
    # needs attention, and a name nobody can link is something to look at.
    return EXIT_UNRESOLVED if (links.unmatched or links.ambiguous) else EXIT_OK


async def run(args: argparse.Namespace) -> int:
    catalogue = read_catalogue(args)

    rows: list[CatalogueRow] = []
    refused: list[str] = []
    for record in catalogue.records:
        try:
            rows.append(to_catalogue_row(record))
        except CatalogueError as exc:
            refused.append(str(exc))

    describe(catalogue, rows, refused)

    if not rows:
        # `HipolabsCatalogue.fetch` already refuses an empty source. This is the
        # same rule one layer up, where the emptiness comes from refusals instead
        # — an upstream key rename refuses every record, and without this the
        # sync mirrors nothing, reports success and leaves a weekly green check
        # over a catalogue that has quietly stopped updating.
        print("\nno usable rows — refusing to sync")
        return EXIT_REFUSED

    if args.dry_run:
        print("\ndry run — nothing written")
        return EXIT_OK

    return await run_sync(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-file", type=Path, help="read a snapshot instead of fetching over HTTPS"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    configure_streams()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
