"""Re-host profile images off Bubble onto Supabase Storage.

    # what would happen, writing nothing
    uv run python scripts/migrate_assets.py --dry-run

    # the real thing, re-runnable
    uv run python scripts/migrate_assets.py

    # one at a time, if a host starts refusing under load
    uv run python scripts/migrate_assets.py --workers 1

**Run it early and often.** The migration package calls this "network-bound,
slow, start day one", and it is the one cutover step whose duration is not ours
to control. It is idempotent, so an early run does most of the work and a later
one catches whatever changed.

Measured on the dev export rather than assumed: 21 assets, 5.7 MiB, averaging
270 KiB, which extrapolates to roughly 154 MiB for 1,200 users. That is minutes,
not hours.

**Two things about Bubble's file host that the tests encode:**

- ``HEAD`` is answered **403** where the identical ``GET`` returns 200, for the
  twelve dev assets behind ``app.edufurther.org``. Nothing here uses ``HEAD``.
- Filenames disagree with their contents — a ``.gif`` and an ``.avif`` in the
  export both hold JPEG bytes — so the media type is sniffed and the extension
  comes from the bytes.

The bucket is created by an operator, once, in the dashboard. This refuses to
start without it rather than creating one, because a script that can create a
public bucket can publish a private one by typo.

Exit codes match the other migration CLIs: 0 clean, 1 refused with nothing done,
2 completed with something a human has to look at.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.domain.assets import (
    AssetAction,
    AssetKind,
    AssetReport,
    decide,
    host_of,
    object_path,
)
from app.domain.images import MIGRATION_ACCEPTED, process
from app.infra.db.asset_store import AssetStore, ProfileAssets
from app.infra.db.engine import resolve_async_dsn
from app.infra.storage.source import AssetSource
from app.infra.storage.supabase import SupabaseStorage

#: Bounded, and small on purpose. The work is network-bound, so some overlap
#: helps; but every worker is a request against someone else's file host, and
#: being rude to Bubble during a cutover is not a trade worth making for a job
#: measured in minutes.
DEFAULT_WORKERS = 4


def build_storage(settings: Settings, http: httpx.Client) -> SupabaseStorage:
    """The Storage adapter, or a message naming exactly what is missing."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise ConfigurationError(
            "asset migration needs SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY. The service-role key is under "
            "Dashboard -> Settings -> API; it is not the anon key."
        )
    return SupabaseStorage(
        base_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
        bucket=settings.supabase_storage_bucket,
        client=http,
    )


def rehost(
    profile: ProfileAssets,
    kind: AssetKind,
    source: AssetSource,
    storage: SupabaseStorage,
) -> str | None:
    """Fetch one image and put it somewhere we own. ``None`` if the source refused.

    Blocking, and called through a thread so several can be in flight. It touches
    no database and no shared state, which is what makes that safe.

    **Legacy images go through the same `process` as an upload**, so there is no
    population of stripped and unstripped images to reason about later. A phone
    photo that reached Bubble still carries the GPS coordinates it was taken at,
    and these profiles are about to become public — ADR 0019 left that open, and
    routing both entry points through one function is what closes it.

    A refusal raises, which `migrate` already reports per image: nothing here has
    to catch it, and an image the decoder will not read must not be silently
    counted as absent.
    """
    url = profile.url_for(kind)
    if url is None:
        return None
    fetched = source.fetch(url)
    if fetched is None:
        return None

    image = process(fetched.payload, kind, accepted=MIGRATION_ACCEPTED)
    # Hashed on what is **stored**, not on what was fetched. Re-running re-fetches
    # the original, re-encodes it to the same bytes and lands on the same path —
    # so a second pass overwrites one object rather than creating a second.
    path = object_path(profile.user_id, kind, image.payload, image.content_type)
    return storage.upload(path, image.payload, image.content_type)


async def migrate(
    store: AssetStore,
    source: AssetSource,
    storage: SupabaseStorage,
    *,
    workers: int,
    dry_run: bool,
) -> AssetReport:
    """Re-host every foreign image, and record where each one went."""
    profiles = await store.profiles()
    jobs = [
        (profile, kind)
        for profile in profiles
        for kind in AssetKind
        if profile.url_for(kind) is not None
    ]
    print(f"{len(profiles)} profiles, {len(jobs)} image URLs")

    copied = skipped = absent = 0
    refused_hosts: set[str] = set()
    failed: list[str] = []
    limit = asyncio.Semaphore(workers)

    async def handle(profile: ProfileAssets, kind: AssetKind) -> tuple[str, str | None]:
        async with limit:
            return kind, await asyncio.to_thread(rehost, profile, kind, source, storage)

    for profile in profiles:
        pending = [
            (kind, decide(profile.url_for(kind), storage.host))
            for kind in AssetKind
            if profile.url_for(kind) is not None
        ]
        to_copy = [kind for kind, action in pending if action is AssetAction.COPY]
        skipped += sum(1 for _, action in pending if action is AssetAction.SKIP)
        refused_hosts.update(
            host_of(profile.url_for(kind))
            for kind, action in pending
            if action is AssetAction.REFUSE
        )

        if not to_copy:
            continue
        if dry_run:
            # Nothing is fetched and nothing is uploaded. The counts are what the
            # real run would do, which is the only thing a rehearsal is for.
            copied += len(to_copy)
            continue

        results = await asyncio.gather(
            *(handle(profile, kind) for kind in to_copy), return_exceptions=True
        )
        for kind, outcome in zip(to_copy, results, strict=True):
            if isinstance(outcome, BaseException):
                # By address and kind: the operator's next action is to look at
                # this one image. The message is the adapter's, which carries a
                # status code and never a request body.
                failed.append(f"{profile.email} {kind}: {outcome}")
                continue
            _, url = outcome
            if url is None:
                absent += 1
                continue
            if await store.record(profile.user_id, kind, url):
                copied += 1
            else:
                failed.append(
                    f"{profile.email} {kind}: uploaded to {url}, but the row would "
                    "not take it — that object is unreferenced"
                )

    report = AssetReport(
        refused_hosts=tuple(sorted(refused_hosts)),
        copied=copied,
        skipped=skipped,
        absent=absent,
        failed=tuple(failed),
    )
    print(("\ndry run — nothing fetched or written\n" if dry_run else "\n") + report.summary())
    for line in report.failures():
        print(line, file=sys.stderr)
    return report


def exit_code(report: AssetReport) -> int:
    """2 when something needs a human, 0 otherwise.

    Never 1 — work was done either way, and 1 is reserved for "refused, nothing
    was written", the distinction the other migration CLIs make.
    """
    return 2 if report.failed else 0


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    engine = create_async_engine(resolve_async_dsn(settings))
    try:
        # Separate clients: the source is anonymous and points at Bubble, and
        # the storage client carries the service-role key. Sharing one would put
        # that key one misconfigured base URL away from a third party.
        with httpx.Client(timeout=60.0) as uploader, httpx.Client(timeout=60.0) as downloader:
            storage = build_storage(settings, uploader)
            storage.ensure_bucket()
            return exit_code(
                await migrate(
                    AssetStore(engine),
                    AssetSource(downloader),
                    storage,
                    workers=args.workers,
                    dry_run=args.dry_run,
                )
            )
    finally:
        await engine.dispose()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"concurrent transfers (default {DEFAULT_WORKERS}; use 1 to be gentle)",
    )
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
