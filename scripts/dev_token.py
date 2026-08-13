"""Mint an access token for calling the local API by hand.

    uv run python scripts/dev_token.py --email ada@example.com
    uv run python scripts/dev_token.py --auth-id 019ff5c8-bb86-7f52-b01f-026ad188b66e
    uv run python scripts/dev_token.py --email ada@example.com --header

The token goes to stdout on its own and everything else to stderr, so the
common use substitutes it directly::

    curl -H "$(uv run python scripts/dev_token.py --email ada@example.com --header)" \
         http://localhost:8000/api/v1/me

**This mints nothing Supabase would accept.** It signs with the symmetric
development secret, and `SupabaseTokenVerifier` prefers JWKS whenever one is
configured — so a real project rejects this outright. That is the intended
ceiling, not a limitation to work around: ADR 0009 gives Supabase the sign-in
path, and `INVITE_ENDPOINTS` exists to prove we never reach the endpoints that
would email 1,200 real people.

**It runs against whatever `DATABASE_URL` is configured**, and names the host it
used on stderr. There is deliberately no local-only restriction: the binding
constraint is the signing scheme, not the database. An environment publishing a
JWKS rejects this token whatever database it was minted against, and an
environment using a shared secret accepts it — so a host check would block a
legitimate lookup while guarding nothing the refusal below does not already.

**Two refusals, and each replaces an unreadable failure.** A configured JWKS URL
means the application verifies asymmetrically and would answer a bare 401; a
missing secret means nothing can be signed at all. Both are better said here than
discovered in the application.

The user is looked up rather than trusted: a token whose subject matches no live
`users.auth_id` is answered by `get_current_user` with a 404, which reads as a
missing endpoint rather than a missing account. Better to say so here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.domain.provisioning import Candidate
from app.infra.auth.dev_tokens import mint_dev_token
from app.infra.db.engine import resolve_async_dsn
from app.infra.db.provisioning_store import ProvisioningStore

#: Longer than the suite's hour: a developer wants a token that lasts the
#: session they are debugging, and re-running the script mid-request is exactly
#: the friction this exists to remove.
DEFAULT_TTL_HOURS = 8


def target_host(settings: Settings) -> str:
    """Where this run looks users up, read from the DSN.

    Reported rather than checked. A flag records what somebody meant; the DSN
    records where the query actually goes, and on a tool that hands out
    credentials the operator should be able to see which it was.
    """
    return urlsplit(resolve_async_dsn(settings)).hostname or "?"


def development_secret(settings: Settings) -> str:
    """The symmetric signing key, or an explanation of what to set.

    A configured JWKS URL is refused rather than ignored: the verifier would
    prefer it, so a token signed here would be rejected by the very application
    this script exists to call.
    """
    if settings.supabase_jwks_url:
        raise ConfigurationError(
            "SUPABASE_JWKS_URL is set, so the application verifies asymmetrically "
            "and would reject a locally signed token. Unset it for local work."
        )
    if settings.supabase_jwt_secret is None:
        raise ConfigurationError(
            "SUPABASE_JWT_SECRET is not set. Set any local value — it signs the "
            "token here and verifies it there, and both sides read the same one."
        )
    return str(settings.supabase_jwt_secret.get_secret_value())


async def find(store: ProvisioningStore, args: argparse.Namespace) -> Candidate:
    """The live user named on the command line, or an error saying which part failed."""
    if args.email:
        found = await store.by_email(args.email)
        missing = f"no live user with email {args.email!r}"
    else:
        found = await store.by_auth_id(args.auth_id)
        missing = f"no live user with auth_id {args.auth_id}"

    if found is None:
        raise ConfigurationError(missing)
    if found.auth_id is None:
        raise ConfigurationError(
            f"{found.email} has no auth_id yet — provision them first with "
            "`scripts/provision_auth.py --link-migrated`."
        )
    return found


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        secret = development_secret(settings)
        host = target_host(settings)
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1

    engine = create_async_engine(resolve_async_dsn(settings))
    try:
        user = await find(ProvisioningStore(engine), args)
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    token = mint_dev_token(
        user.auth_id,
        secret=secret,
        ttl=timedelta(hours=args.ttl_hours),
        email=user.email,
    )
    print(
        f"{user.email}  ->  {user.auth_id}   valid {args.ttl_hours}h   via {host}",
        file=sys.stderr,
    )
    print(f"Authorization: Bearer {token}" if args.header else token)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--email", help="the user's address")
    who.add_argument("--auth-id", type=UUID, help="the user's Supabase identifier")
    parser.add_argument(
        "--ttl-hours", type=int, default=DEFAULT_TTL_HOURS, help="how long the token lasts"
    )
    parser.add_argument("--header", action="store_true", help="print a whole Authorization header")
    args = parser.parse_args(argv)
    if args.email:
        args.email = args.email.strip().lower()
    return args


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
