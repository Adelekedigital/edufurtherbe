"""Sign an end-to-end runner in as a seeded E2E account, on staging only.

    E2E_ALLOW=1 railway run uv run python scripts/e2e_session.py \\
        --env staging --account mentee_with_credits --token
    E2E_ALLOW=1 railway run uv run python scripts/e2e_session.py \\
        --env staging --account mentee_with_credits --link --redirect <frontend callback>

`--token` prints an access token for the API runner; `--link` prints a one-time
magic link for the browser runner. That is the only thing on stdout; what
happened goes to stderr, and **neither is ever logged**.

**A real Supabase session, made here rather than on a server.** The admin
`generate_link` sends no email (Supabase: "Generates email links and OTPs to be
sent via a custom email provider"); the token comes from exchanging its hash at
`/auth/v1/verify`, as `verifyOtp` does. Nothing is added to the deployed API,
so no sign-in shortcut exists where an attacker could reach it.

**Refused unless** `E2E_ALLOW` is on, `--env staging` is given,
`EDUFURTHER_ENVIRONMENT` is explicitly `staging` (`refuse_e2e_session`),
`SUPABASE_URL` names the `E2E_SUPABASE_PROJECT` project, and the account is
one of `E2E_ACCOUNTS` for the run (`--run`, the tag `seed_demo_mentors.py
--e2e` was given) on the reserved `.test` domain, checked again inside the
client before any request.

`dev_token.py` signs locally and is refused wherever JWKS verifies;
`supabase_otp.py` needs an inbox, which a `.test` address does not have. This
is the third case: a deployed environment and an address nobody can receive.

The account's Supabase identity is created silently when missing, and a seeded
row (`seed_demo_mentors.py --e2e`) is pointed at it. An account the seed does
not create (`mentee_new`) is left for its first sign-in to make.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.auth.admin import AdminApiError
from app.infra.auth.e2e_sessions import E2E_ENVIRONMENTS, E2ESessionClient, refuse_e2e_session
from app.infra.db.demo_seed import (
    E2E_ACCOUNTS,
    E2ERunError,
    e2e_account_seeded,
    e2e_email,
    link_e2e_account,
)
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

TIMEOUT = httpx.Timeout(30.0)


async def _in_session[T](work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    engine = create_database_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            result = await work(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _seeded(email: str) -> bool:
    return await _in_session(lambda session: e2e_account_seeded(session, email))


async def _link(email: str, auth_id: UUID) -> bool:
    return await _in_session(lambda session: link_e2e_account(session, email, auth_id))


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if (why := refuse_e2e_session(settings, args.env)) is not None:
        print(why, file=sys.stderr)
        return EXIT_REFUSED

    try:
        email = e2e_email(args.account, args.run)
    except E2ERunError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    if settings.supabase_url is None or settings.supabase_service_role_key is None:
        return EXIT_REFUSED  # refused above; narrows the types
    with httpx.Client(timeout=TIMEOUT) as http:
        client = E2ESessionClient(
            base_url=settings.supabase_url,
            service_role_key=settings.supabase_service_role_key.get_secret_value(),
            client=http,
        )
        seeded = E2E_ACCOUNTS[args.account].seeded
        # Checked first, so a mistyped `--run` creates no Supabase identity.
        if seeded and not asyncio.run(_seeded(email)):
            print(
                f"{args.account} is not seeded for this run; run seed_demo_mentors.py --e2e <run>",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        try:
            identity = client.ensure_account(email)
            if seeded and not asyncio.run(_link(email, identity.id)):
                print(f"{args.account} could not be linked", file=sys.stderr)
                return EXIT_REFUSED
            link = client.magic_link(email, redirect_to=args.redirect)
            secret = client.access_token(link) if args.token else link.action_link
        except AdminApiError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
    print(
        f"{args.account}: {'access token' if args.token else 'magic link'} ready", file=sys.stderr
    )
    print(secret)
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, choices=sorted(E2E_ENVIRONMENTS))
    parser.add_argument("--run", required=True, help="The run tag the accounts were seeded with.")
    parser.add_argument("--account", required=True, choices=sorted(E2E_ACCOUNTS))
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--token", action="store_true", help="Print an access token.")
    what.add_argument("--link", action="store_true", help="Print a one-time magic link.")
    parser.add_argument("--redirect", help="Where the magic link lands (the frontend callback).")
    return parser.parse_args(argv)


def main() -> int:
    configure_streams()
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
