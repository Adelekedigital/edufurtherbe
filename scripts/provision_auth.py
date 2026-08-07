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

**Provisioning is eager** (ADR 0018, closing the question ADR 0014 left open).
The short version: eager is the only variant you can prove works *before* the
freeze — it runs end to end against a rehearsal database and reports a count.

**Every mode is idempotent, and that is the recovery plan** (ADR 0018 §2). There
is no undo for creating 1,200 auth accounts, so instead of a rollback, a failed
run is re-run.
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
from collections.abc import Sequence
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.domain.enums import AdminRole, PrimaryRole
from app.domain.provisioning import Action, Lookup, Outcome, plan_for
from app.infra.auth.admin import SupabaseAdminClient
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.provisioning_store import ProvisioningStore


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


def lookup_with(client: SupabaseAdminClient) -> Lookup:
    """Adapt the Admin API to what ``domain.provisioning`` asks for.

    The domain wants ``str -> UUID | None``; the adapter returns its own
    ``AuthUser``. Narrowing happens here, in the composition root, because that is
    where a vendor shape is allowed to be known — it is the same translation
    ``api/deps.py`` performs for a token subject.
    """

    def lookup(email: str) -> UUID | None:
        found = client.find_by_email(email)
        return found.id if found else None

    return lookup


async def link_migrated(
    store: ProvisioningStore, client: SupabaseAdminClient, *, dry_run: bool
) -> Outcome:
    """Provision every live user that has never authenticated.

    Returns the ``Outcome`` rather than an exit code so a caller — and a
    test — can assert what the run *planned*, not only what it refrained
    from doing. A dry-run test that checks absence alone passes identically
    against a run that planned nothing at all.
    """
    candidates = await store.candidates()
    print(f"{len(candidates)} live users")
    lookup = lookup_with(client)

    created = linked = skipped = 0
    failed: list[str] = []

    for candidate in candidates:
        try:
            plan = plan_for(candidate, lookup)

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
                if await store.link(candidate.user_id, auth_id):
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
            elif await store.link(candidate.user_id, plan.existing_auth_id):
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


async def verify(store: ProvisioningStore, client: SupabaseAdminClient) -> int:
    """Confirm every ``auth_id`` we hold still names a real Supabase account.

    ADR 0014 names this as its weakest point: nothing else checks that the two
    systems still agree. A user whose Supabase account was deleted looks perfectly
    normal in our database and cannot log in, and the first report of it is a
    support ticket.

    **This covers `users` -> Supabase and not the reverse** (ADR 0018 §3). An auth
    account created by a `--create` run that died before its `INSERT` is invisible
    here, and is recovered only by re-running `--create` for the same address.
    """
    rows = await store.linked()
    print(f"{len(rows)} linked users")
    problems: list[str] = []

    for _user_id, email, auth_id in rows:
        try:
            account = client.get(auth_id)
        except Exception as error:
            problems.append(f"{email}: {error}")
            continue

        if account is None:
            problems.append(f"{email}: auth_id {auth_id} is not in Supabase")
        elif account.email.lower() != email:
            # Not fatal, and worth knowing: someone changed their address on one
            # side only, so a password reset goes to an address we do not show.
            problems.append(f"{email}: Supabase holds {account.email.lower()}")

    for problem in problems:
        print(f"   MISMATCH {problem}", file=sys.stderr)
    print(f"\n{len(rows) - len(problems)} verified, {len(problems)} to look at")
    return 2 if problems else 0


async def create(
    store: ProvisioningStore,
    client: SupabaseAdminClient,
    *,
    email: str,
    role: PrimaryRole,
    name: str | None,
    dry_run: bool,
) -> int:
    """Create one account and the ``users`` row for it."""
    existing = await store.by_email(email)
    if existing is not None:
        print(
            f"{email} already exists here"
            + ("" if existing.auth_id else " — use --link-migrated to provision it"),
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
        user_id = await store.insert_user(email=email, first_name=name, role=role, auth_id=auth_id)
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


async def grant_admin(
    store: ProvisioningStore, *, email: str, role: AdminRole, dry_run: bool
) -> int:
    """Grant an administrative role to a user that already exists.

    **This is the bootstrap path, and it has no authorization check of its own.**
    There is no first admin to approve the first admin, so the authority here is
    **possession of the database credentials, and only those** — this mode never
    contacts Supabase and needs no service-role key. Requiring one would document
    a control the system does not have, since anyone holding the DSN can insert
    the row by hand.

    Every grant lands in ``admin_users`` with ``granted_by`` null, which is the
    honest record of "granted out of band" rather than a missing value.
    """
    user = await store.by_email(email)
    if user is None:
        print(f"no live user with address {email}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"dry run — would grant {role} to {email}")
        return 0

    granted = await store.grant_admin(user.user_id, role)
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
    store = ProvisioningStore(engine)

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
                store,
                email=args.email,
                role=admin_role(args.role),
                dry_run=args.dry_run,
            )

        with httpx.Client(timeout=30.0) as http:
            client = build_client(settings, http)

            if args.link_migrated:
                return exit_code(await link_migrated(store, client, dry_run=args.dry_run))
            if args.verify:
                return await verify(store, client)
            return await create(
                store,
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
