# EduFurther Backend

Backend API for [EduFurther](https://edufurther.org) — a mentorship platform
connecting African students pursuing international graduate school with mentors
at institutions worldwide.

This service is replacing a Bubble low-code application. The frontend is a
separate Next.js app that consumes this API.

## Quickstart

```bash
uv sync --all-extras --dev      # install
uv run pre-commit install       # install the git hooks (pre-commit + commit-msg)
cp .env.example .env            # local settings, including the database URL
docker compose up -d            # PostgreSQL 17 on port 55432
uv run alembic upgrade head     # apply the migration chain
make check                      # the full local gate — run before every commit
```

On Windows, where `make` is usually absent, run the gate directly:

```bash
uv run python scripts/check.py
```

Run the API:

```bash
uv run uvicorn app.main:app --reload --app-dir src
```

Then `GET http://127.0.0.1:8000/health` returns `{"status": "ok"}`, and the
generated OpenAPI schema is at `/docs`.

## Layout

```
src/app/api/       transport only — no business logic
src/app/domain/    the product — pure Python, no I/O, no framework
src/app/infra/     adapters — DB, cache, outbound clients
src/app/core/      configuration and the base error taxonomy
tests/{unit,integration,e2e}/
docs/adr/          decision records — read before proposing a rewrite
docs/edufurther-migration/   the target schema, field mapping and runbook —
                   received from the migration work, never edited here
migrations/        the Alembic chain — outside src/ so it is neither packaged
                   into the wheel nor scanned by the layer check
alembic.ini        migration config; carries no database URL, by design
scripts/           the layer check, the local gate, and the reference-data
                   generator whose output is committed as a migration
```

`api/deps.py` and `main.py` are the only sanctioned wiring points.

## The rule that matters most

`domain/` imports no framework and no other layer except `core/`. It is enforced
by `scripts/check_layers.py`, which runs in pre-commit and in CI. Do not weaken
`[tool.check-layers]` in `pyproject.toml` to make a failing check pass — if a
boundary genuinely needs to move, that is an ADR.

Booking rules, availability, pricing, and fee splits belong in `domain/`
precisely because they are the code most worth testing without a database.

## Configuration

All configuration flows through `src/app/core/config.py`, read from environment
variables or a local `.env`.

Only `EDUFURTHER_ENVIRONMENT` and `EDUFURTHER_DEBUG` carry a prefix — both are
generic enough that a host may already define them. Everything else
(`DATABASE_URL`, `SUPABASE_URL`, `CORS_ORIGINS`, …) names its own subject and
stands alone.

A leftover `EDUFURTHER_` key from before that change fails at startup and names
its replacement. A misspelled unprefixed key cannot be detected — it looks like
any other variable — so the setting silently keeps its default. Secrets are
`SecretStr` and are never logged.

`.env` is git-ignored. So are `data/`, `exports/`, and `*.csv`. Those cover file
output; the Bubble extract itself now lands in a `staging` schema inside the
database ([ADR 0007](docs/adr/0007-adopt-the-migration-package-as-the-target-data-model.md)),
so it holds 1,200 users' PII somewhere `.gitignore` cannot reach. Keeping it out
of the repository is still necessary and is no longer sufficient.

## The database

PostgreSQL, reached through SQLAlchemy 2.0 with `asyncpg`. **Alembic is the chain
of record**; the DDL in `docs/edufurther-migration/schema/` is a specification
that is read and transcribed, never executed ([ADR 0011](docs/adr/0011-alembic-is-the-migration-chain.md)).

```bash
docker compose up -d                          # start PostgreSQL 17 locally
uv run alembic upgrade head                   # apply migrations
uv run alembic revision -m "what it does"     # new migration, hand-written
uv run alembic downgrade -1                   # step back one
```

Migrations run as a separate deploy step, never on application startup — startup
migrations race across replicas.

### Reference data

`countries` (ISO 3166-1, 249 rows) and `languages` (ISO 639-3, 7,078 living
languages) are seeded by a migration. Both are foreign-key targets — six columns
across identity and profiles reference `countries(code)` — so they must be
complete before any user data loads.

The rows are **embedded in the migration**, not read from a data file. A
migration must produce the same result whenever it runs; one that reads a file
produces whatever the file says at the time, so a fresh environment and an
existing one silently diverge the moment anyone regenerates it.

```bash
uv run python scripts/generate_reference_seeds.py \
    --revision <new-id> --down-revision <head> --output migrations/versions/<name>.py
```

The annual ISO refresh is therefore a **new migration**, never an edit to a
shipped one. Do not hand-edit the generated file — change the generator and
regenerate.

Rows are derived from [`pycountry`](https://pypi.org/project/pycountry/), which
packages the Debian [`iso-codes`](https://salsa.debian.org/iso-codes-team/iso-codes)
database (LGPL-2.1-or-later), publishing the ISO 3166-1 country codes and the
SIL-maintained ISO 639-3 language codes. `pycountry` is a **development**
dependency only: the data is baked into the migration, so nothing needs it at
runtime.

ISO 639-3 rather than 639-1 because the two-letter set covers ~184 languages and
omits Nigerian Pidgin (`pcm`) entirely, which for this platform's market is not
an acceptable gap. Macrolanguages (`ara`, `swa`) are kept deliberately — for
"what languages do you speak", that is the right granularity.

### Tests that need a database

Tests marked `db` read `TEST_DATABASE_URL`. Each one creates a database of its
own and drops it afterwards, so they are safe to run against the local instance
and are order-independent under `pytest-randomly`.

```bash
export TEST_DATABASE_URL=postgresql://edufurther:edufurther@localhost:55432/edufurther
uv run pytest
```

The full gate runs pytest with four `pytest-xdist` workers. Each worker migrates
one session template, then every database-backed test clones its own database
from that template and drops it afterwards. This preserves per-test isolation
without replaying the migration chain for every test. Four workers is an
intentional ceiling: `-n auto` can turn a machine's extra cores into PostgreSQL
create/drop contention. A direct `uv run pytest` remains sequential, which is
useful when diagnosing an order or concurrency failure.

The synchronized Windows baseline before parallel execution was 22m36s for
2,334 passing tests. Four workers completed the same database-backed gate in
16m25s on that machine, a 27% wall-time reduction. Treat those as comparison
points, not permanent budgets; the gate reports its own elapsed time on every
run.

**Without that variable they skip**, with a message saying what to set — no
Docker required to run the rest of the suite. CI sets `REQUIRE_DB_TESTS=1`, which
turns that skip into a failure: a skipped test and a passing test look identical
in a summary line, so without it the entire database tier could disappear and the
check would stay green.

## Stack

The pre-cutover set — what the initial build targets. This table is a record of
decisions, not of code, and it grows as each piece lands.

**Three of the rows below are built; the rest are decisions with no code behind
them yet.** PostgreSQL through SQLAlchemy and asyncpg, Supabase Auth as a token
verifier (`infra/auth/`), and Supabase Storage for profile images
(`infra/storage/`) — all three per [ADR 0005](docs/adr/0005-data-platform.md).
On top of them sit the migration ETL for milestones 1–4 and the read API:
profiles, catalogues, availability, sessions, and a public surface a mentee can
browse without an account.

Everything else in the table — calendar, video, analytics, PDF, push, LLM — is
recorded and unwritten.

| Concern | Choice | Notes |
|---|---|---|
| Database, auth, file storage | Supabase | Used *as Postgres* — no PostgREST, no RLS as application logic ([ADR 0005](docs/adr/0005-data-platform.md)) |
| Hosting | FastAPI Cloud | Railway is the named fallback; a standalone Dockerfile keeps the exit real |
| Scheduled and background jobs | Upstash QStash | HTTP delivery, so no always-on worker process |
| Transactional email | Emailit | Behind a port, like every vendor — this one has already been swapped twice |
| Video sessions | Daily | |
| Product analytics | PostHog | |
| Google Calendar | Direct, behind a `CalendarPort` | Our own OAuth client; write events, read free/busy on demand ([ADR 0004](docs/adr/0004-calendar-integration.md)). The scopes we need are non-sensitive, so there is no review to survive and no platform to pay for it ([ADR 0012](docs/adr/0012-google-oauth-scopes-and-client-split.md)) |
| HTML to PDF and image | MarkupGo | |
| Institution data | hipolabs | Autocomplete served live; a row is stored only once someone selects it, so reads never depend on it ([ADR 0008](docs/adr/0008-institutions-hipolabs-registry.md)) |
| Push notifications | Native Web Push | VAPID and a service worker, no vendor. iOS delivers only to installed PWAs |
| LLM access | Provider-agnostic port | No gateway — a port gives swappability without flattening provider features |

WhatsApp, prompt tooling, payments, in-app messaging and internal analytics are
deliberately absent: they land after the cutover and are recorded in
[`docs/adr/`](docs/adr/README.md) as they are decided.

Each is to be reached through a Protocol in `domain/ports.py`, implemented in
`infra/` and wired in `api/deps.py`, so that `domain/` never learns a vendor's
name. That file does not exist yet — it appears with the first adapter.

## Contributing

Conventional Commits are enforced by a `commit-msg` hook; the release tooling
parses them. Work happens on branches — direct commits to `main` are blocked
locally.

See `CLAUDE.md` for the agent-facing router and the project's non-negotiables.
