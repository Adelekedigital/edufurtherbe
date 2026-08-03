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
scripts/           the layer check and the local gate
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

All configuration flows through `src/app/core/config.py`, read from
`EDUFURTHER_`-prefixed environment variables or a local `.env`. A misspelled
prefixed variable fails at startup rather than silently leaving a default in
place. Secrets are `SecretStr` and are never logged.

`.env` is git-ignored. So are `data/`, `exports/`, and `*.csv`. Those cover file
output; the Bubble extract itself now lands in a `staging` schema inside the
database ([ADR 0007](docs/adr/0007-adopt-the-migration-package-as-the-target-data-model.md)),
so it holds 1,200 users' PII somewhere `.gitignore` cannot reach. Keeping it out
of the repository is still necessary and is no longer sufficient.

## Stack

The pre-cutover set — what the initial build targets. **Nothing here is
integrated yet**: `src/` currently holds configuration, the error taxonomy and a
health endpoint. This table is a record of decisions, not of code, and it grows
as each piece lands.

| Concern | Choice | Notes |
|---|---|---|
| Database, auth, file storage | Supabase | Used *as Postgres* — no PostgREST, no RLS as application logic ([ADR 0005](docs/adr/0005-data-platform.md)) |
| Hosting | FastAPI Cloud | Railway is the named fallback; a standalone Dockerfile keeps the exit real |
| Scheduled and background jobs | Upstash QStash | HTTP delivery, so no always-on worker process |
| Transactional email | Emailit | Behind a port, like every vendor — this one has already been swapped twice |
| Video sessions | Daily | |
| Product analytics | PostHog | |
| Google Calendar | Composio | Our own OAuth client; write events, read free/busy on demand ([ADR 0004](docs/adr/0004-calendar-integration.md)) |
| HTML to PDF and image | MarkupGo | |
| Institution data | ROR | Synced from ROR's data dump; hipolabs is the fallback and the source of email domains |
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
