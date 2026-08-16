"""The development token CLI, against a real database.

**The refusals are the tests that matter.** This script mints a credential, so
the interesting question is never whether it can — it is whether it declines in
every environment it should. Each refusal below was watched failing before the
guard existed.

The happy path is asserted through the **real** `SupabaseTokenVerifier` and then
through a real request, rather than by decoding the token here. Decoding it with
`jwt.decode` would test this file's understanding of its own encoder, which is
the one thing that cannot be wrong.
"""

from types import ModuleType
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import SECRET, bearer

from app.core.config import Settings
from app.infra.auth.supabase import SupabaseTokenVerifier

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LOCAL_DSN = "postgresql+asyncpg://edufurther:edufurther@localhost:55432/edufurther"
REMOTE_DSN = "postgresql+asyncpg://postgres.abcdefghijklmnop:pw@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"


def settings_for(dsn: str, **overrides: object) -> Settings:
    """Settings as a developer's machine would hold them.

    ``supabase_jwt_secret`` is a default rather than a fixed value, so a test
    about the signing key can supply its own without colliding with this one.
    """
    return Settings(
        _env_file=None,
        database_url=dsn,
        **{"supabase_jwt_secret": SECRET, **overrides},  # type: ignore[arg-type]
    )


async def seed(engine: AsyncEngine, email: str, *, auth_id: UUID | None) -> UUID:
    async with engine.begin() as connection:
        return (
            await connection.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()


def dsn_of(engine: AsyncEngine) -> str:
    """The test database's DSN *with* its password.

    ``str(engine.url)`` masks it as ``***`` — which reads as a working DSN and
    fails at connect time with an authentication error, several layers from the
    thing that removed it.
    """
    return engine.url.render_as_string(hide_password=False)


def args_for(script: ModuleType, *argv: str) -> object:
    return script.parse_args(list(argv))


# --------------------------------------------------------------------------
# The happy path — a token the application actually accepts
# --------------------------------------------------------------------------


async def test_a_minted_token_verifies_through_the_real_verifier(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_id = uuid4()
    await seed(db_engine, "verified@example.test", auth_id=auth_id)
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    code = await dev_token_script.run(
        args_for(dev_token_script, "--email", "verified@example.test")
    )

    assert code == 0
    token = capsys.readouterr().out.strip()
    assert SupabaseTokenVerifier(secret=SECRET).verify(token).subject == auth_id


async def test_the_token_opens_an_authenticated_endpoint(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The criterion the whole script exists for: a real request returns 200."""
    auth_id = uuid4()
    await seed(db_engine, "opens@example.com", auth_id=auth_id)
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))
    await dev_token_script.run(args_for(dev_token_script, "--email", "opens@example.com"))
    token = capsys.readouterr().out.strip()

    response = await api_client.get("/api/v1/me", headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["email"] == "opens@example.com"


async def test_the_auth_id_route_finds_the_same_user(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two ways in, one lookup each, both live-scoped."""
    auth_id = uuid4()
    await seed(db_engine, "byauth@example.test", auth_id=auth_id)
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    code = await dev_token_script.run(args_for(dev_token_script, "--auth-id", str(auth_id)))

    assert code == 0
    assert SupabaseTokenVerifier(secret=SECRET).verify(capsys.readouterr().out.strip()).subject == (
        auth_id
    )


async def test_the_header_form_is_pasteable(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await seed(db_engine, "header@example.test", auth_id=uuid4())
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    await dev_token_script.run(
        args_for(dev_token_script, "--email", "header@example.test", "--header")
    )

    assert capsys.readouterr().out.startswith("Authorization: Bearer ey")


async def test_the_host_it_used_is_reported(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """There is no local-only restriction, so the host is stated instead.

    The signing scheme is the binding constraint, not the database: a JWKS
    environment rejects this token whatever it was minted against, and a
    shared-secret one accepts it. A host check would block a legitimate lookup
    while guarding nothing `development_secret` does not already. What an
    operator does need is to see which database handed over the identity.
    """
    await seed(db_engine, "reported@example.test", auth_id=uuid4())
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    code = await dev_token_script.run(
        args_for(dev_token_script, "--email", "reported@example.test")
    )

    assert code == 0
    assert f"via {db_engine.url.host}" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


async def test_a_configured_jwks_url_is_refused(
    dev_token_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Otherwise the token is minted, rejected, and debugged in the wrong place.

    Given an unreachable remote DSN on purpose: the refusal has to land before
    anything connects, or a misconfigured environment costs a timeout before it
    costs an explanation.
    """
    monkeypatch.setattr(
        dev_token_script,
        "get_settings",
        lambda: settings_for(REMOTE_DSN, supabase_jwks_url="https://x.supabase.co/keys"),
    )

    code = await dev_token_script.run(args_for(dev_token_script, "--email", "a@b.test"))

    assert code == 1
    assert "verifies asymmetrically" in capsys.readouterr().err


async def test_the_jwks_refusal_does_not_advise_unsetting_a_deployed_setting(
    dev_token_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The message is read most often in the case where its old advice was wrong.

    An earlier version ended "Unset it for local work." Run through `railway run`
    — which injects a deployed environment into a local process — that reads as
    an instruction to disable asymmetric verification in staging. The refusal has
    to name the other situation and the real alternative instead.
    """
    monkeypatch.setattr(
        dev_token_script,
        "get_settings",
        lambda: settings_for(REMOTE_DSN, supabase_jwks_url="https://x.supabase.co/keys"),
    )

    await dev_token_script.run(args_for(dev_token_script, "--email", "a@b.test"))

    message = capsys.readouterr().err
    assert "Supabase" in message, "must name where a real token comes from"
    assert "deployed" in message, "must distinguish local from deployed"
    assert "Unset it for local work" not in message


async def test_a_missing_secret_is_refused(
    dev_token_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        dev_token_script,
        "get_settings",
        lambda: Settings(_env_file=None, database_url=LOCAL_DSN),
    )

    code = await dev_token_script.run(args_for(dev_token_script, "--email", "a@b.test"))

    assert code == 1
    assert "SUPABASE_JWT_SECRET" in capsys.readouterr().err


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_a_blank_secret_is_refused(
    dev_token_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blank: str,
) -> None:
    """Set-but-blank is missing, and the first version of this guard missed it.

    It asked `is None`, and `SUPABASE_JWT_SECRET=` parses to `SecretStr('')`,
    which is not None — so the script would have signed a token with an empty
    key while the application refused to start at all. `engine.py` already treats
    a set-but-empty `DATABASE_URL` this way; this brings the odd one out into
    line, and it is the only place in the codebase that used identity rather
    than truthiness on a `SecretStr`.
    """
    monkeypatch.setattr(
        dev_token_script,
        "get_settings",
        lambda: Settings(_env_file=None, database_url=LOCAL_DSN, supabase_jwt_secret=blank),
    )

    code = await dev_token_script.run(args_for(dev_token_script, "--email", "a@b.test"))

    assert code == 1
    assert "SUPABASE_JWT_SECRET" in capsys.readouterr().err


async def test_a_secret_with_surrounding_whitespace_is_not_trimmed(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guard validates on the stripped value and returns the raw one.

    The application resolves the same setting through its own code and does not
    trim. Trimming here would sign with `padded` while the verifier holds
    `"  padded  "` — every token rejected, with a bare 401 and nothing pointing
    at the cause. Asserted through the real verifier holding the *untrimmed*
    key, which is the only thing that distinguishes the two.
    """
    padded = "  a-local-signing-secret-with-padding  "
    auth_id = uuid4()
    await seed(db_engine, "padded@example.test", auth_id=auth_id)
    monkeypatch.setattr(
        dev_token_script,
        "get_settings",
        lambda: settings_for(dsn_of(db_engine), supabase_jwt_secret=padded),
    )

    code = await dev_token_script.run(args_for(dev_token_script, "--email", "padded@example.test"))

    assert code == 0
    token = capsys.readouterr().out.strip()
    assert SupabaseTokenVerifier(secret=padded).verify(token).subject == auth_id


async def test_an_unknown_user_is_refused(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    code = await dev_token_script.run(args_for(dev_token_script, "--email", "nobody@example.test"))

    assert code == 1
    assert "no live user" in capsys.readouterr().err


@pytest.mark.parametrize("by", ["email", "auth-id"])
async def test_a_soft_deleted_user_is_refused(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    by: str,
) -> None:
    """A soft-deleted user keeps their `auth_id`, so **both** lookups need `LIVE`.

    Parametrised because the first version of this test asked only by email, and
    `BY_EMAIL` already carried the predicate — so the one added to `BY_AUTH_ID`
    could be deleted with the file green. A second lookup path is where a
    predicate goes missing, and that is the shape every public-endpoint bug in
    this milestone has had.

    Without it the script hands a working credential to somebody the rest of the
    system treats as gone.
    """
    auth_id = uuid4()
    email = f"gone-{by}@example.test"
    await seed(db_engine, email, auth_id=auth_id)
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE email = :e"), {"e": email}
        )
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))
    handle = ("--email", email) if by == "email" else ("--auth-id", str(auth_id))

    code = await dev_token_script.run(args_for(dev_token_script, *handle))

    assert code == 1
    assert "no live user" in capsys.readouterr().err


async def test_an_unprovisioned_user_is_refused(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `auth_id` means no subject to sign, and `provision_auth` is the fix."""
    await seed(db_engine, "unprovisioned@example.test", auth_id=None)
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    code = await dev_token_script.run(
        args_for(dev_token_script, "--email", "unprovisioned@example.test")
    )

    assert code == 1
    assert "provision_auth" in capsys.readouterr().err


async def test_the_signing_secret_is_never_printed(
    dev_token_script: ModuleType,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await seed(db_engine, "quiet@example.test", auth_id=uuid4())
    monkeypatch.setattr(dev_token_script, "get_settings", lambda: settings_for(dsn_of(db_engine)))

    await dev_token_script.run(args_for(dev_token_script, "--email", "quiet@example.test"))

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
