"""Exchange an email OTP for a real Supabase access token.

    railway run uv run python scripts/supabase_otp.py --email you+27@gmail.com
    railway run uv run python scripts/supabase_otp.py --email you+27@gmail.com --code 123456

The token goes to stdout on its own and everything else to stderr, so the common
use substitutes it directly::

    curl -H "$(railway run uv run python scripts/supabase_otp.py \
                 --email you+27@gmail.com --code 123456 --header)" \
         https://edufurtherbe-dev.up.railway.app/api/v1/me

**This is the counterpart to `dev_token.py`, not a replacement for it.**
`dev_token.py` signs with the symmetric development secret, and
`SupabaseTokenVerifier` prefers JWKS whenever one is configured — so against
staging or Railway its tokens are rejected by design. This script mints nothing
itself: it asks Supabase, and what comes back is what a real sign-in produces.

**It never touches the Admin API.** `infra/auth/admin.py` fences off
`/auth/v1/invite` and `/auth/v1/admin/generate_link` because provisioning runs
against 1,200 real people and a wrong endpoint delivers 1,200 emails with no
undo. `generate_link` would be more convenient here — no inbox round trip — and
that convenience is exactly what the fence is protecting. This uses the same
public endpoints a browser would.

**Tracked, though it ships with nothing.** It is a development tool run by hand
against an environment the operator has already authenticated to, and is
imported by no application module — but a tool that only exists on one machine
is a tool the next person rediscovers by writing it again.

It reads ``SUPABASE_ANON_KEY`` from the environment directly, which is the one
place in this repository that does. Non-negotiable #6 routes configuration
through ``core/config.py``, and this variable deliberately is not there: the
application holds the service-role key and says *"never the anon key"*, which is
correct for it and leaves this script with nothing to read. ``scripts/`` is a
composition root, so the exception is local and stated rather than a precedent.

Prerequisites, both one-time:

1. The **Magic Link** email template must contain ``{{ .Token }}``. Supabase
   sends a link rather than a code until it does — the endpoint is the same
   either way, and only the template decides. Authentication -> Email Templates.
2. ``SUPABASE_ANON_KEY`` must be set. Neither Railway nor any `.env` in this
   repository carries it: `config.py` holds the service-role key and says
   "Never the anon key", which is correct for the application and leaves this
   script with nothing to read. Add it once with::

       railway variables --set "SUPABASE_ANON_KEY=<the anon public key>"

   It is a publishable key — the one a browser is meant to hold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx

SEND = "/auth/v1/otp"
VERIFY = "/auth/v1/verify"

#: Supabase calls this "email" for a code delivered by email. The code itself is
#: `token` rather than `code`, which is the field name most likely to be guessed
#: wrong.
VERIFY_TYPE = "email"

TIMEOUT = httpx.Timeout(30.0)


class RefusedError(RuntimeError):
    """Supabase refused, or the environment cannot support the call."""


def _env(name: str, hint: str) -> str:
    raw = os.environ.get(name, "")
    if not raw.strip():
        raise RefusedError(f"{name} is not set, or is blank.\n{hint}")
    return raw


def _settings() -> tuple[str, str]:
    url = _env(
        "SUPABASE_URL",
        "Run under `railway run` so the service's variables are injected, or export it.",
    )
    key = _env(
        "SUPABASE_ANON_KEY",
        "No .env in this repository defines it and Railway does not either — the\n"
        "application only ever holds the service-role key. Add it once:\n\n"
        '    railway variables --set "SUPABASE_ANON_KEY=<the anon public key>"\n\n'
        "It is in the dashboard under Project Settings -> API, marked `anon` `public`.",
    )
    return url.rstrip("/"), key


def _post(url: str, key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{url}{path}",
        headers={"apikey": key, "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}

    if response.status_code >= 400:
        raise RefusedError(f"{path} returned {response.status_code}\n{json.dumps(body, indent=2)}")
    return body if isinstance(body, dict) else {"raw": body}


def send(url: str, key: str, email: str) -> None:
    _post(url, key, SEND, {"email": email})
    print(f"asked Supabase to send a code to {email}", file=sys.stderr)
    print(
        "Supabase answers the same way whether or not the address has an account,\n"
        "so a 2xx here is not evidence the mail was addressable.\n"
        "If a link arrives instead of a code, the Magic Link template is missing "
        "`{{ .Token }}`.",
        file=sys.stderr,
    )


def verify(url: str, key: str, email: str, code: str, *, as_header: bool) -> None:
    body = _post(url, key, VERIFY, {"type": VERIFY_TYPE, "email": email, "token": code})

    token = body.get("access_token")
    if not token:
        raise RefusedError(
            f"verified, but the response carried no access_token:\n{json.dumps(body, indent=2)}"
        )

    user = body.get("user") or {}
    print(f"subject {user.get('id', '(unknown)')} <{user.get('email', email)}>", file=sys.stderr)
    print(f"expires in {body.get('expires_in', '?')}s", file=sys.stderr)

    print(f"Authorization: Bearer {token}" if as_header else token)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send an email OTP, then exchange the code for an access token.",
        epilog="Without --code the script sends. With --code it verifies.",
    )
    parser.add_argument("--email", required=True, help="the address to sign in as")
    parser.add_argument("--code", help="the six digits from the email")
    parser.add_argument(
        "--header",
        action="store_true",
        help="print `Authorization: Bearer ...` rather than the bare token",
    )
    args = parser.parse_args()

    try:
        url, key = _settings()
        print(f"supabase: {urlsplit(url).hostname}", file=sys.stderr)

        if args.code:
            verify(url, key, args.email, args.code, as_header=args.header)
        else:
            send(url, key, args.email)
    except RefusedError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"\ncould not reach Supabase: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
