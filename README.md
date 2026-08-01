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

`.env` is git-ignored. So are `data/`, `exports/`, and `*.csv`, which is where
Bubble migration output lands — it contains member PII and must never enter the
repository history.

## Contributing

Conventional Commits are enforced by a `commit-msg` hook; the release tooling
parses them. Work happens on branches — direct commits to `main` are blocked
locally.

See `CLAUDE.md` for the agent-facing router and the project's non-negotiables.
