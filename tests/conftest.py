"""Fixtures shared across every tier."""

import asyncio
import importlib.util
import io
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg
import httpx
import jwt
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.infra.auth.admin import SupabaseAdminClient
from app.infra.auth.supabase import SupabaseTokenVerifier
from app.infra.db.engine import create_database_engine, create_session_factory
from app.infra.db.provisioning_store import ProvisioningStore
from app.infra.storage.supabase import SupabaseStorage
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Test-harness variables, not application configuration — which is why they are
# read here with `os.environ` rather than added to `Settings`. The
# no-inline-os.environ house rule governs application config.
#
# `TEST_` keeps them clear of the real settings: `DATABASE_URL` is now a live
# unprefixed field, so a bare name here would collide with the thing under test.
DB_URL_ENV = "TEST_DATABASE_URL"
REQUIRE_DB_ENV = "REQUIRE_DB_TESTS"

_SKIP_REASON = (
    f"No database. Start one with `docker compose up -d` and set "
    f"{DB_URL_ENV}=postgresql://edufurther:edufurther@localhost:55432/edufurther"
)


@pytest.fixture
def settings() -> Settings:
    """Explicit settings, so a test never depends on the developer's environment.

    ``_env_file=None`` is load-bearing. Without it the fixture still reads
    whatever ``.env`` happens to sit in the working directory, and any field not
    pinned below silently takes that file's value — init arguments outrank a
    dotenv value, so the fields named here hid the leak rather than preventing it.
    """
    return Settings(_env_file=None, environment="ci", debug=False)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# The ASGI client every API test shares
#
# These began in `test_api_me.py`. A second API test file needed the same
# fixture, the same signing key and the same seeded user, and copying them is
# the case non-negotiable #8 names: a test double that exists in two places is a
# defect, because the copies drift and the drifted one still passes.
# --------------------------------------------------------------------------

#: Local signing key, used only to mint tokens for tests. Flagged by S105 as a
#: hardcoded credential, which is exactly what it is — a test that verified
#: signatures against a real key would be testing the key, not the verifier.
SECRET = "test-signing-secret"  # noqa: S105
PROBLEM_JSON = "application/problem+json"


def api_token(subject: str | uuid.UUID, *, secret: str = SECRET, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": str(subject),
        "aud": "authenticated",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "email": "someone@example.com",
    }
    return jwt.encode(claims | overrides, secret, algorithm="HS256")


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture
def api_storage() -> SupabaseStorage | None:
    """No bucket by default; overridden by the tests that upload.

    A default of `None` rather than a working fake keeps `app.state.storage`
    unset everywhere else — so a route that reaches storage without meaning to
    fails loudly instead of quietly writing into a test double.
    """
    return None


@pytest_asyncio.fixture
async def api_client(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> AsyncIterator[httpx.AsyncClient]:
    """An app bound to this test's disposable database and a local signing key.

    Both are injected through ``app.state`` rather than the process-wide caches,
    so nothing here mutates state another test would inherit.

    ``httpx.AsyncClient`` over ASGI rather than ``TestClient``: the sync client
    drives the app on its own event loop, while ``db_engine`` is bound to the one
    pytest-asyncio is running. asyncpg notices, and the failure reads "attached
    to a different loop" — which sounds like an asyncpg bug and is a test-harness
    one.
    """
    app = create_app(Settings(_env_file=None))
    app.state.session_factory = create_session_factory(db_engine)
    app.state.token_verifier = SupabaseTokenVerifier(secret=SECRET)
    if api_storage is not None:
        app.state.storage = api_storage

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _sync_dsn(url: str) -> str:
    """Strip any SQLAlchemy driver suffix — asyncpg.connect wants a bare DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    """The maintenance connection, or skip.

    ``REQUIRE_DB_TESTS=1`` turns the skip into a failure. CI sets it, so a
    misconfigured runner cannot quietly report green with the entire database
    tier absent — a skipped test and a passing test are indistinguishable in a
    summary line, and this repository has been bitten by that shape before.
    """
    url = os.environ.get(DB_URL_ENV)
    if url:
        return _sync_dsn(url)
    if os.environ.get(REQUIRE_DB_ENV) == "1":
        pytest.fail(f"{REQUIRE_DB_ENV}=1 but {DB_URL_ENV} is unset. {_SKIP_REASON}")
    pytest.skip(_SKIP_REASON)


@pytest.fixture
def disposable_database(admin_database_url: str) -> Iterator[str]:
    """An empty database of its own, dropped afterwards.

    Per-test rather than shared because ``pytest-randomly`` reorders the suite:
    a migration test that downgrades a database another test expects at head
    fails only on some seeds, which is the worst kind of flake to diagnose.

    ``CREATE DATABASE`` cannot run inside a transaction or take bind parameters,
    hence the raw connection and the interpolated name. The name is built here
    from a uuid4 hex string, so it is never caller-controlled.
    """
    name = f"test_{uuid.uuid4().hex[:16]}"
    base, _, _ = admin_database_url.rpartition("/")

    async def _create() -> None:
        conn = await asyncpg.connect(admin_database_url)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def _drop() -> None:
        conn = await asyncpg.connect(admin_database_url)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    try:
        yield f"{base}/{name}"
    finally:
        asyncio.run(_drop())


def _alembic_config(url: str) -> Config:
    """An Alembic config pointed at ``url``.

    The URL travels on ``config.attributes``, which ``env.py`` prefers over the
    setting. Nothing here touches ``os.environ`` or the ``get_settings`` cache:
    a process-wide mutation would outlive the test that made it, and under
    ``pytest-randomly`` the test it then affects is different on every seed.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["dsn"] = url
    return config


@pytest.fixture
def make_alembic_config() -> Callable[[str], Config]:
    """Exposed as a fixture because ``tests`` is not an importable package.

    There is no ``tests/__init__.py``, and adding one to share a helper would
    change import semantics for every module in the suite. A fixture is the
    smaller change.
    """
    return _alembic_config


@pytest.fixture
def migrated_database(disposable_database: str) -> Iterator[str]:
    """A disposable database with the full migration chain applied.

    Synchronous on purpose: ``env.py`` calls ``asyncio.run``, which raises if a
    loop is already running. Keeping every alembic invocation in sync fixtures
    and sync tests is what makes that safe.
    """
    command.upgrade(_alembic_config(disposable_database), "head")
    yield disposable_database


@pytest_asyncio.fixture
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """An engine bound to a migrated, disposable database.

    ``Settings`` is constructed directly rather than read from the environment,
    so this fixture neither depends on nor disturbs the process environment.
    """
    engine = create_database_engine(
        Settings(_env_file=None, database_url=SecretStr(migrated_database))
    )
    try:
        yield engine
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# One way to make a user, for every schema test that needs one
#
# Lives here rather than in a test module because a second suite now needs it,
# which is the extract-on-the-second-occurrence case non-negotiable #8 names.
# The hazard is specific rather than aesthetic: a private copy that supplied its
# own `id` would keep passing after somebody removed the `uuid_generate_v7()`
# default, and nothing would say so. One definition, one behaviour.
# --------------------------------------------------------------------------

INSERT_USER = """
INSERT INTO users (email, slug, deleted_at)
VALUES (:email, :slug, :deleted_at)
RETURNING id
"""


async def add_user(
    conn: AsyncConnection,
    email: str,
    *,
    slug: str | None = None,
    deleted_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a user and return the id the database generated.

    The id is **not** supplied. ADR 0014 made it ours and gave it back its
    ``uuid_generate_v7()`` default, so letting the column fill itself is both
    what production does and a standing check that the default is still there —
    a test that passed its own id would keep passing after somebody removed it.

    ``auth_id`` is left null, which is the state every migrated user starts in.
    """
    created = await conn.execute(
        text(INSERT_USER),
        {"email": email, "slug": slug, "deleted_at": deleted_at},
    )
    return uuid.UUID(str(created.scalar_one()))


# --------------------------------------------------------------------------
# One canonical Bubble user record, for every suite that needs one
#
# Second consumer, so it moves here — the same reason `add_user` did. A private
# copy would drift on exactly the fields a transform reads, and the divergence
# would look like a difference in the code under test.
# --------------------------------------------------------------------------

USER_ID = "1701974206179x877854702892984200"
PROFILE_ID = "1761272910139x746213933324959700"


def user(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bubble_id": USER_ID,
        "email": "sakiratu@example.com",
        "👥Role": "Mentor",
        "UserTimezonID": "Africa/Lagos",
        "created_at": "2023-12-07T18:36:46.179Z",
        "modified_at": "2025-11-06T11:52:41.383Z",
        "User-last-onboarding-step": "5",
        "registration completed ": "2023-12-07T18:38:28.221Z",
        "👤Personal Info": PROFILE_ID,
        "Registration format": "Email",
        "provider_identities": {},
    }
    return base | overrides


# --------------------------------------------------------------------------
# Provisioning: one Supabase double, and one representation of its paging
#
# There were three copies of "how GoTrue answers a list request" — one in the
# integration suite, one in the unit suite, one in the stress harness — and all
# three omitted `per_page`, which is why a client asking for a single row passed
# every test while permanently stranding any user whose address is a substring of
# a newer one. The third copy was written as the *fix* for that defect and had it
# too. The paging rule now exists once.
#
# These live in the root conftest rather than beside either suite because
# `tests/` has no `__init__.py`; a fixture is the sanctioned way to share a
# helper across files here, which is the same reason `make_alembic_config` is one.
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def gotrue_page(rows: list[dict[str, str]], request: httpx.Request) -> httpx.Response:
    """Slice an ordered result set the way GoTrue's admin list endpoint does.

    Newest-first is the caller's job; paging is this function's. A short page
    means "no more", which is the signal the client stops on.
    """
    page = int(request.url.params.get("page", 1))
    per_page = int(request.url.params.get("per_page", 50))
    start = (page - 1) * per_page
    return httpx.Response(200, json={"users": rows[start : start + per_page]})


class FakeSupabase:
    """Enough of the Admin API to be re-run against.

    Stateful rather than a stub sequence, because every property worth testing
    here is about what a *second* run does: it must cost nothing, find what the
    first run created, and never make a second account for one address.

    ``fail_for`` makes one address error, which is how "a bad user does not end
    the run" is tested without waiting for a real one.
    """

    def __init__(self, *, fail_for: str | None = None) -> None:
        self.accounts: dict[str, uuid.UUID] = {}
        self.creates: list[str] = []
        self.lookups: list[str] = []
        self.fail_for = fail_for

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return self._create(request)
        # `GET /admin/users` is a search; `GET /admin/users/{id}` is a fetch.
        tail = request.url.path.rsplit("/", 1)[-1]
        return self._find(request) if tail == "users" else self._get(tail)

    def _create(self, request: httpx.Request) -> httpx.Response:
        email = json.loads(request.content)["email"]
        self.creates.append(email)
        if email == self.fail_for:
            return httpx.Response(500, json={"msg": "boom"})
        if email in self.accounts:
            return httpx.Response(422, json={"error_code": "email_exists"})
        self.accounts[email] = uuid.uuid4()
        return httpx.Response(200, json={"id": str(self.accounts[email]), "email": email})

    def _find(self, request: httpx.Request) -> httpx.Response:
        needle = request.url.params.get("filter", "")
        self.lookups.append(needle)
        if needle == self.fail_for:
            return httpx.Response(500, json={"msg": "boom"})
        # Substring match, newest-first — insertion order reversed stands in for
        # age. Both halves matter: the substring is what makes a one-row page
        # return the wrong account, and newest-first is what puts it there.
        matches = [
            {"id": str(identifier), "email": address}
            for address, identifier in reversed(list(self.accounts.items()))
            if needle in address
        ]
        return gotrue_page(matches, request)

    def _get(self, identifier: str) -> httpx.Response:
        for address, known in self.accounts.items():
            if str(known) == identifier:
                return httpx.Response(200, json={"id": identifier, "email": address})
        return httpx.Response(404)

    def client(self) -> SupabaseAdminClient:
        return SupabaseAdminClient(
            base_url="https://project.supabase.co",
            service_role_key="test-key",
            client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            sleep=lambda _: None,
        )


@pytest.fixture
def fake_supabase() -> type[FakeSupabase]:
    """The stateful Admin API double, as a class so a test can configure it."""
    return FakeSupabase


@pytest.fixture
def gotrue_paging() -> Callable[[list[dict[str, str]], httpx.Request], httpx.Response]:
    """The paging rule alone, for tests that script responses rather than state."""
    return gotrue_page


@pytest.fixture
def provision_script() -> ModuleType:
    """``scripts/provision_auth.py``, which is a script rather than a module."""
    spec = importlib.util.spec_from_file_location(
        "provision_auth", PROJECT_ROOT / "scripts" / "provision_auth.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def common_languages_migration() -> ModuleType:
    """The migration holding the `is_common` seed.

    Loaded by path for the reason `provision_script` is: a revision filename
    starts with a date and is not an importable module name. The point is to
    compare the literal that shipped against what actually landed in the table —
    the first version of that literal was six codes wrong, because it was
    written out by hand rather than pasted from the derivation script.
    """
    path = next((PROJECT_ROOT / "migrations" / "versions").glob("*_common_lang_*.py"))
    spec = importlib.util.spec_from_file_location("common_languages_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def store(db_engine: AsyncEngine) -> ProvisioningStore:
    """The provisioning store, bound to this test's disposable database."""
    return ProvisioningStore(db_engine)


# ---------------------------------------------------------------------------
# images and storage
# ---------------------------------------------------------------------------
#
# **Real encoded images, not header bytes.** The migration and the upload
# endpoint both decode what they are given now, so a JFIF magic number followed
# by zeroes is refused rather than stored — which is correct behaviour and was
# how the first version of this suite reported a bug in the code that was in the
# fixture. `Image.new` costs microseconds at these sizes.

SUPABASE_URL = "https://project.supabase.co"
STORAGE_BUCKET = "profile-images"

#: A GPS position, in the EXIF rational form a phone actually writes. Nairobi.
_GPS = {
    1: "S",
    2: (1.0, 17.0, 0.0),
    3: "E",
    4: (36.0, 49.0, 0.0),
}


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (40, 30), *, gps: bool = False) -> bytes:
    """One real image, encoded. ``gps`` writes the coordinates a camera would."""
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (10, 120, 200))
    if gps:
        exif = Image.Exif()
        exif[0x8825] = _GPS
        image.save(buffer, format=fmt, exif=exif)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


class FakeStorage:
    """Enough of Supabase Storage to be re-run against.

    Method-aware, because delete is now part of the contract: an upload that
    counted a `DELETE` as an upload would report every replacement as two
    objects and hide a cleanup that never ran.
    """

    def __init__(self, *, bucket_exists: bool = True, upload_fails: bool = False) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.bucket_exists = bucket_exists
        self.upload_fails = upload_fails

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/storage/v1/bucket/"):
            return httpx.Response(200 if self.bucket_exists else 404, json={})
        path = request.url.path.split(f"/storage/v1/object/{STORAGE_BUCKET}/", 1)[-1]
        if request.method == "DELETE":
            self.deletes.append(path)
            if path not in self.objects:
                return httpx.Response(404, json={})
            del self.objects[path]
            return httpx.Response(200, json={})
        if self.upload_fails:
            return httpx.Response(500, json={"message": "storage is down"})
        self.uploads.append(path)
        self.objects[path] = (request.content, request.headers.get("Content-Type", ""))
        return httpx.Response(200, json={"Key": path})


def storage_for(fake: FakeStorage) -> SupabaseStorage:
    """The **real** adapter over a fake transport. A hand-written stand-in would
    test the stand-in, and the retry and status handling are the parts that
    have been wrong before."""
    return SupabaseStorage(
        base_url=SUPABASE_URL,
        service_role_key="test-key",
        bucket=STORAGE_BUCKET,
        client=httpx.Client(transport=httpx.MockTransport(fake.handle)),
        sleep=lambda _: None,
    )
