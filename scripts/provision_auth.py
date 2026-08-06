"""Give migrated users a Supabase account, and create or elevate new ones.

    # what would happen, writing to neither Supabase nor the database
    # (it reads both: one Admin API lookup per unlinked user, so budget for it)
    uv run python scripts/provision_auth.py --link-migrated --dry-run

    # the cutover run
    uv run python scripts/provision_auth.py --link-migrated

    # confirm every auth_id we hold still exists in Supabase
    uv run python scripts/provision_auth.py --verify

    # a new account, after cutover
    uv run python scripts/provision_auth.py --create --email a@b.com --role mentor

    # the bootstrap grant, and every one after it
    uv run python scripts/provision_auth.py --grant-admin --email a@b.com --role super_admin

**Provisioning is eager.** ADR 0014 left it open between provisioning everything
before cutover and linking lazily at each first login; ``domain/provisioning.py``
records why eager won. The short version: eager is the only version you can prove
works *before* the freeze.

**Every mode is idempotent, and that is the recovery plan.** There is no undo for
creating 1,200 auth accounts, so instead of a rollback, a failed run is re-run.
A user already linked costs zero API calls the second time; an account created by
a run that died before recording it is found by address and linked. Nothing here
retries a create by itself for the same reason — see ``_send`` in the adapter.

**The service-role key.** This is the only entry point that holds it. It bypasses
row-level security and can delete any user, so it is read through ``SecretStr``,
never printed, and never included in an error message.

Exit codes follow ``load_identity.py``: 0 clean, 1 refused with nothing done,
2 completed with something an operator has to look at.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.domain.enums import AdminRole, PrimaryRole
from app.domain.provisioning import Action, Candidate, Outcome, Plan, decide
from app.infra.auth.admin import AuthUser, SupabaseAdminClient
from app.infra.db.engine import resolve_async_dsn
from app.infra.etl.loader import timestamps_from_source

# `deleted_at IS NULL` is in the query rather than checked after, per
# non-negotiable #5 — a soft-deleted user must not be given a live account.
CANDIDATES = text("SELECT id, email, auth_id FROM users WHERE deleted_at IS NULL ORDER BY id")

LINKED = text(
    "SELECT id, email, auth_id FROM users "
    "WHERE auth_id IS NOT NULL AND deleted_at IS NULL ORDER BY id"
)

BY_EMAIL = text("SELECT id, email, auth_id FROM users WHERE email = :email AND deleted_at IS NULL")

# Both guards are load-bearing, and neither is a millisecond window.
#
# `auth_id IS NULL` makes a second concurrent run a no-op on rows the first
# already linked, rather than overwriting one Supabase identifier with another —
# which would orphan an account nothing can reach.
#
# `deleted_at IS NULL` is the same predicate `CANDIDATES` carries, repeated on the
# write. The candidate list is read once and the run then takes minutes to hours,
# so a user soft-deleted while it is in flight would otherwise be handed a live
# account from a plan built when they were live.
LINK = text(
    "UPDATE users SET auth_id = :auth_id "
    "WHERE id = :user_id AND auth_id IS NULL AND deleted_at IS NULL"
)

INSERT_USER = text(
    "INSERT INTO users (email, first_name, primary_role, auth_id) "
    "VALUES (:email, :first_name, :primary_role, :auth_id) RETURNING id"
)

# The partial unique index is the conflict target, so re-granting a role someone
# already holds is a no-op while a *revoked* historical row does not block the
# re-grant. `granted_by` stays null: a CLI run has no acting user, and a
# synthetic one would look like knowledge we do not have.
GRANT = text(
    "INSERT INTO admin_users (user_id, admin_role) VALUES (:user_id, :admin_role) "
    "ON CONFLICT (user_id, admin_role) WHERE revoked_at IS NULL DO NOTHING RETURNING id"
)


def build_client(settings: Settings, http: httpx.Client) -> SupabaseAdminClient:
    """Construct the Admin API adapter, or say exactly what is missing."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise ConfigurationError(
            "provisioning needs EDUFURTHER_SUPABASE_URL and "
            "EDUFURTHER_SUPABASE_SERVICE_ROLE_KEY. The service-role key is under "
            "Dashboard -> Settings -> API; it is not the anon key."
        )
    return SupabaseAdminClient(
        base_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
        client=http,
    )


def plan_for(candidate: Candidate, lookup: Callable[[str], AuthUser | None]) -> Plan:
    """Decide one candidate, consulting Supabase only when the decision needs it.

    The guard repeats ``decide``'s first branch on purpose. ``decide`` is pure and
    cannot make the call itself, and doing the lookup unconditionally would spend
    1,200 API calls on a re-run where every answer is already known — turning the
    cheap resume path into the expensive one.
    """
    if candidate.auth_id is not None:
        return decide(candidate, None)
    found = lookup(candidate.email)
    return decide(candidate, found.id if found else None)


async def apply(engine: AsyncEngine, plan: Plan, auth_id: UUID) -> bool:
    """Record the link, preserving Bubble's ``updated_at``.

    **The trigger has to be held off.** ``updated_at`` carries Bubble's Modified
    Date for every migrated row — that is what M1c went to some trouble to load —
    and ``trg_set_updated_at`` fires on any ``UPDATE``. Without the disable, a
    provisioning run silently rewrites 1,200 modification dates to the moment
    provisioning happened, one day after the migration that preserved them.

    Linking an auth identifier is not a modification of the user's data, so the
    honest value is the one already there.

    One short transaction per user: the API call has already happened outside it,
    so the ACCESS EXCLUSIVE lock the disable takes is held for a single ``UPDATE``
    rather than across network I/O. Committing per user is also what makes a
    partial run resumable — work already done stays done.
    """
    async with engine.begin() as connection:
        async with timestamps_from_source(connection, "users"):
            result = await connection.execute(
                LINK, {"auth_id": auth_id, "user_id": plan.candidate.user_id}
            )
        # 0 means another run linked this row between the read and the write. The
        # account is provisioned either way, so this is not a failure.
        return result.rowcount > 0


async def link_migrated(
    engine: AsyncEngine, client: SupabaseAdminClient, *, dry_run: bool
) -> Outcome:
    """Provision every live user that has never authenticated.

    Returns the ``Outcome`` rather than an exit code so a caller — and a
    test — can assert what the run *planned*, not only what it refrained
    from doing. A dry-run test that checks absence alone passes identically
    against a run that planned nothing at all.
    """
    async with engine.connect() as connection:
        rows = (await connection.execute(CANDIDATES)).mappings().all()

    candidates = [
        Candidate(user_id=row["id"], email=row["email"], auth_id=row["auth_id"]) for row in rows
    ]
    print(f"{len(candidates)} live users")

    created = linked = skipped = 0
    failed: list[str] = []

    for candidate in candidates:
        try:
            plan = plan_for(candidate, client.find_by_email)

            if plan.action is Action.SKIP:
                skipped += 1
                continue

            if dry_run:
                # Reads have happened; nothing that changes state will.
                if plan.action is Action.CREATE:
                    created += 1
                else:
                    linked += 1
                continue

            if plan.action is Action.CREATE:
                auth_id = client.create_user(candidate.email).id
                if await apply(engine, plan, auth_id):
                    created += 1
                else:
                    # We made an account and the row would not take it — linked by
                    # a concurrent run, or soft-deleted mid-run. Either way this
                    # account is now referenced by nothing, and dropping it from
                    # the counts would stop them summing to the population above.
                    failed.append(
                        f"{candidate.email}: created auth account {auth_id}, but the "
                        "row would not take it — that account is unreferenced"
                    )
            elif plan.existing_auth_id is None:
                # Unreachable while `decide` holds — LINK is only returned with
                # an id. Raised rather than asserted so it stays true under -O,
                # and so this user is reported instead of ending the run.
                raise RuntimeError("LINK without an existing auth id")
            elif await apply(engine, plan, plan.existing_auth_id):
                linked += 1
            else:
                # The account already existed in Supabase and the row is already
                # linked to it. Nothing was created, so nothing is orphaned.
                skipped += 1
        except Exception as error:
            # By address, because the operator's next action is to look at this
            # user. `error` is the adapter's message, which carries a status code
            # and never a request body — the request carried the service key.
            failed.append(f"{candidate.email}: {error}")

    outcome = Outcome(created=created, linked=linked, skipped=skipped, failed=tuple(failed))
    print(("\ndry run — nothing written\n" if dry_run else "\n") + outcome.summary())
    for line in outcome.failures():
        # stderr, matching `load_identity.py`: anything needing attention survives
        # a caller keeping stdout for the summary.
        print(line, file=sys.stderr)
    return outcome


def exit_code(outcome: Outcome) -> int:
    """2 when something needs a human, 0 otherwise.

    Never 1: work was done either way, and 1 is reserved for "refused, nothing
    was written" — the distinction `load_identity.py` makes and a runbook needs.
    """
    return 2 if outcome.failed else 0


async def verify(engine: AsyncEngine, client: SupabaseAdminClient) -> int:
    """Confirm every ``auth_id`` we hold still names a real Supabase account.

    ADR 0014 names this as its weakest point: nothing else checks that the two
    systems still agree. A user whose Supabase account was deleted looks perfectly
    normal in our database and cannot log in, and the first report of it is a
    support ticket.
    """
    async with engine.connect() as connection:
        rows = (await connection.execute(LINKED)).mappings().all()

    print(f"{len(rows)} linked users")
    problems: list[str] = []

    for row in rows:
        try:
            account = client.get(row["auth_id"])
        except Exception as error:
            problems.append(f"{row['email']}: {error}")
            continue

        if account is None:
            problems.append(f"{row['email']}: auth_id {row['auth_id']} is not in Supabase")
        elif account.email.lower() != row["email"]:
            # Not fatal, and worth knowing: someone changed their address on one
            # side only, so a password reset goes to an address we do not show.
            problems.append(f"{row['email']}: Supabase holds {account.email.lower()}")

    for problem in problems:
        print(f"   MISMATCH {problem}", file=sys.stderr)
    print(f"\n{len(rows) - len(problems)} verified, {len(problems)} to look at")
    return 2 if problems else 0


async def create(
    engine: AsyncEngine,
    client: SupabaseAdminClient,
    *,
    email: str,
    role: PrimaryRole,
    name: str | None,
    dry_run: bool,
) -> int:
    """Create one account and the ``users`` row for it."""
    async with engine.connect() as connection:
        existing = (await connection.execute(BY_EMAIL, {"email": email})).mappings().first()

    if existing is not None:
        print(
            f"{email} already exists here"
            + ("" if existing["auth_id"] else " — use --link-migrated to provision it"),
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(f"dry run — would create {email} as {role}")
        return 0

    # Supabase first. If the insert then fails, a re-run finds this account by
    # address rather than making a second one; doing it the other way round
    # leaves a row nobody can log in as.
    auth_id = client.create_user(email).id

    try:
        async with engine.begin() as connection:
            user_id = (
                await connection.execute(
                    INSERT_USER,
                    {
                        "email": email,
                        "first_name": name,
                        "primary_role": role.value,
                        "auth_id": auth_id,
                    },
                )
            ).scalar_one()
    except Exception as error:
        # The account exists and nothing references it. Exit 1 means "refused,
        # nothing was written", which is the one thing that is not true here.
        #
        # The likeliest cause is a **soft-deleted** row still holding this
        # address's account: `users.auth_id` is plain UNIQUE, not partial, so the
        # deleted row keeps it forever while `BY_EMAIL` filters that row out.
        print(
            f"created auth account {auth_id} for {email}, then could not write the "
            f"user row: {error}",
            file=sys.stderr,
        )
        return 2

    print(f"created {email} as {role} (user {user_id})")
    return 0


async def grant_admin(engine: AsyncEngine, *, email: str, role: AdminRole, dry_run: bool) -> int:
    """Grant an administrative role to a user that already exists.

    **This is the bootstrap path, and it has no authorization check of its own.**
    There is no first admin to approve the first admin, so the authority here is
    possession of the database credentials and the service-role key. Every grant
    lands in ``admin_users`` with ``granted_by`` null, which is the honest record
    of "granted out of band" rather than a missing value.
    """
    async with engine.connect() as connection:
        user = (await connection.execute(BY_EMAIL, {"email": email})).mappings().first()

    if user is None:
        print(f"no live user with address {email}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"dry run — would grant {role} to {email}")
        return 0

    async with engine.begin() as connection:
        granted = (
            await connection.execute(GRANT, {"user_id": user["id"], "admin_role": role.value})
        ).first()

    print(f"granted {role} to {email}" if granted else f"{email} already holds {role}")
    return 0


def refuse_role(vocabulary: type[StrEnum]) -> NoReturn:
    """Name the legal values rather than leaving the operator guessing."""
    raise SystemExit(f"--role must be one of: {', '.join(member.value for member in vocabulary)}")


def primary_role(value: str) -> PrimaryRole:
    """``--create`` takes a primary role.

    Two functions rather than one generic helper: ``--role`` means a different
    vocabulary in each mode, argparse cannot express a mode-dependent ``choices``,
    and a single parser typed over both would have to lie about its return type.
    """
    try:
        return PrimaryRole(value)
    except ValueError:
        refuse_role(PrimaryRole)


def admin_role(value: str) -> AdminRole:
    """``--grant-admin`` takes an administrative one."""
    try:
        return AdminRole(value)
    except ValueError:
        refuse_role(AdminRole)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(resolve_async_dsn(settings))

    # `httpx.Client` is synchronous, so these calls block the event loop. That is
    # correct here rather than tolerated: this is a single-purpose CLI with
    # nothing else on the loop, and an async client would add a second
    # concurrency model to a script whose whole safety argument rests on doing
    # one user at a time, in order.
    try:
        if args.grant_admin:
            # The only mode that never talks to Supabase, so it opens no client
            # and needs no service-role key. Tier-2 row 41 says the authority for
            # a grant is the database credential, and this is what makes that
            # true rather than merely written down.
            return await grant_admin(
                engine,
                email=args.email,
                role=admin_role(args.role),
                dry_run=args.dry_run,
            )

        with httpx.Client(timeout=30.0) as http:
            client = build_client(settings, http)

            if args.link_migrated:
                return exit_code(await link_migrated(engine, client, dry_run=args.dry_run))
            if args.verify:
                return await verify(engine, client)
            return await create(
                engine,
                client,
                email=args.email,
                role=primary_role(args.role),
                name=args.name,
                dry_run=args.dry_run,
            )
    finally:
        await engine.dispose()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--link-migrated",
        action="store_true",
        help="provision every live user that has never authenticated",
    )
    mode.add_argument("--create", action="store_true", help="create one new user")
    mode.add_argument("--grant-admin", action="store_true", help="grant an administrative role")
    mode.add_argument("--verify", action="store_true", help="check our auth_ids against Supabase")

    parser.add_argument("--email", help="required by --create and --grant-admin")
    parser.add_argument(
        "--role", help="mentee|mentor for --create; an admin role for --grant-admin"
    )
    parser.add_argument("--name", help="optional first name, for --create")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if (args.create or args.grant_admin) and not (args.email and args.role):
        parser.error("--create and --grant-admin both need --email and --role")
    if (args.link_migrated or args.verify) and (args.email or args.role or args.name):
        # `--verify --email ada@example.com` reads as "verify this one user". It
        # does not, and accepting the flag silently is how an operator comes to
        # believe it did.
        parser.error("--link-migrated and --verify take no --email, --role or --name")
    # Lowercased here so the CHECK on `users.email` is satisfied by construction
    # rather than by the operator remembering — normalisation at the boundary,
    # and this is the boundary (ADR 0016).
    if args.email:
        args.email = args.email.strip().lower()
    return args


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
