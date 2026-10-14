# Runbook: go-live (production cutover)

Bring up production for the first time. Do the steps in order; each one assumes
the one before it is done. Anything the repo cannot prove is marked
**Owner to confirm**.

Pairs with [`rollback.md`](rollback.md) and [`backup-restore.md`](backup-restore.md).
The data cutover itself (freeze, export, load) is ADR 0003 and the migration
package, not this page.

## 1. Create the production environments

| What | Where | State today |
|---|---|---|
| Railway production service | Railway console | Not in the repo. There is no `railway.json`, so start command and healthcheck live in the console |
| Supabase production project | Supabase dashboard | Not in the repo (ADR 0005) |
| GitHub `production` environment | Repo settings → Environments | Exists, deployment branches limited to `main` (GitHub API, 2026-10-12) |

1. Create the Railway service from this repo. Start command, the same as the
   `development` service runs (read from its settings, 2026-10-14, #452):
   `uvicorn src.app.main:app --host 0.0.0.0 --port 8080`. Railpack installs with
   `uv sync --locked --no-dev`, so nothing in the dev group reaches the image.
2. Point the Railway deploy healthcheck at **`/ready`**, not `/health`.
   `/ready` runs `SELECT 1` with a 2-second timeout and answers `503` when the
   database does not, so a release that cannot reach its database gets no
   traffic; `/health` checks nothing (decision 243,
   `src/app/api/routes/health.py`). Railway config as code is not in the repo
   yet, so this is a console step.
3. In the Supabase production project, configure Auth SMTP through Emailit in
   the console; the service does not send the sign-in code itself
   (`loops_api_key` comment, `src/app/core/config.py`).
4. Create the private intake bucket named by `SUPABASE_INTAKE_BUCKET`.
   **Owner to confirm:** bucket name and that it is private.

## 2. Set the Railway production variables

Set `EDUFURTHER_ENVIRONMENT=production`. The web app then **refuses to start**
and names every gap at once until all of these are set (decision 238,
`production_gaps` in `src/app/core/config.py`, called from `create_app` in
`src/app/main.py`):

| Required | Variables |
|---|---|
| Database, auth | `DATABASE_URL`, `SUPABASE_URL`, and `SUPABASE_JWKS_URL` **or** `SUPABASE_JWT_SECRET` |
| Email | `LOOPS_API_KEY` (plus `EMAIL_TEMPLATES`, see step 7) |
| Video | `DAILY_API_KEY`, `DAILY_WEBHOOK_SECRET` (step 6) |
| Scheduler | `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY` |
| URLs | `PUBLIC_BASE_URL` (this API's origin), `APP_BASE_URL` (the front end) |
| Platform calendar | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_CALENDAR_REFRESH_TOKEN` |
| Mentor calendar | `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `CALENDAR_TOKEN_KEY` |
| Storage | `SUPABASE_INTAKE_BUCKET` |
| Errors | `SENTRY_DSN` (ADR 0030) |
| Also checked | `CORS_ORIGINS` non-empty; `EDUFURTHER_DEBUG` off |

`DAILY_WEBHOOK_SECRET` is in the list, so the app will not start in production
until step 6 has a secret. **Owner to confirm:** whether to register the webhook
before the first deploy, or set the secret first and register straight after.

`DATABASE_URL`: statement and lock timeouts are applied as a plain `SET`, which
works on a direct connection or a session-mode pooler, **not** the transaction
pooler (ADR 0005, open question "Pooling mode"). **Owner to confirm:** the
production DSN form.

## 3. Set the GitHub `production` environment

| Workflow | Reads |
|---|---|
| `migrate.yml` | secret `DATABASE_URL` |
| `reconcile-qstash-schedules.yml` | secret `QSTASH_TOKEN`; vars `PUBLIC_BASE_URL`, `QSTASH_URL`, `QSTASH_SCHEDULE_OVERRIDES` |
| `settle-sessions.yml` | secrets `DATABASE_URL`, `LOOPS_API_KEY`, `DAILY_API_KEY`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_CALENDAR_REFRESH_TOKEN`; vars `EMAIL_TEMPLATES`, `APP_BASE_URL`, `GOOGLE_OAUTH_CLIENT_ID` |
| `credit-reminders`, `expire-credits`, `monthly-credits`, `sync-institutions` | secret `DATABASE_URL` |

Every recovery workflow takes an `environment` input (`staging` or
`production`), and a production job raises `ConfigurationError` rather than run
on a null notifier, calendar or rooms adapter (`_live_in_production` in
`src/app/infra/jobs/runner.py`). Without the providers above, a production
`settle-sessions` dispatch refuses to start; that is the safe failure.

GitHub runners and the Supabase DSN: **Owner to confirm** that the production
`DATABASE_URL` secret is reachable from a GitHub-hosted runner (the repo does
not record which DSN form works there).

## 4. Migrate

1. From `main`, dispatch **Migrate** (`.github/workflows/migrate.yml`) with
   `target=production`. The workflow refuses production from any other ref, and
   the environment's branch policy allows only `main`.
2. Read the run log: it prints `alembic current` before and after. The "after"
   revision must be the head on `main`.
3. Never migrate on app startup (ADR 0017 Decision 4, ADR 0028 Decision 2). Migrate **before**
   deploying code that needs the new schema (`project-conventions`, migration
   ordering).

A migration that waits on a lock fails after 10 seconds (`migrations/env.py`,
decision 238). Re-dispatch it; do not raise the timeout.

## 5. Enable the schedules

Production schedules ship **disabled** (ADR 0028 Decision 6;
`config/runtime-schedules.json`, every job `"production": false`). Merging code
never changes QStash.

1. Set the GitHub `production` variable `QSTASH_SCHEDULE_OVERRIDES` to a JSON
   object keyed by job name. Only `enabled` (boolean) and `cron` (string) are
   accepted; anything else fails the run (`_validated_overrides`,
   `src/app/infra/jobs/manifest.py`; ADR 0028 Decision 5). To enable all seven:

   ```json
   {"settle-sessions":{"enabled":true},"credit-reminders":{"enabled":true},"monthly-credits":{"enabled":true},"expire-credits":{"enabled":true},"sync-institutions":{"enabled":true},"refresh-next-available":{"enabled":true},"sweep-intake-files":{"enabled":true}}
   ```

   **Owner to confirm:** that all seven go live at cutover.
2. Dispatch **Reconcile QStash runtime schedules** with
   `environment=production`, `apply=false`. Read the printed diff: schedule ids
   are `edufurther-production-<job>` and destinations start with
   `PUBLIC_BASE_URL`.
3. Dispatch again with `apply=true`.

## 6. Register the Daily webhook (#405)

1. In Daily, register a webhook to `POST {PUBLIC_BASE_URL}/api/v1/callbacks/daily`
   (`src/app/api/routes/callbacks.py`).
2. Set `DAILY_WEBHOOK_SECRET` on Railway to the HMAC secret Daily issues:
   base64, at least 16 bytes (validator in `src/app/core/config.py`).
3. Close #405. The Daily account is shared with the legacy app (#405), so this
   is a cutover step, not earlier.

## 7. Loops

1. Set `EMAIL_TEMPLATES` (Railway and the GitHub `production` variable) to the
   production map. The variable reference and the current ids are in
   `docs/loops-template-variables.md`; the owner's live registry is linked from
   `docs/handoff-email-templates.md`.
2. Session Reminder copy says "5 minutes"; the backend cutoff is 10 (#400).
   Change it when the legacy app stops sending. **Owner to confirm:** whether
   production maps `session_reminder` to the legacy template
   (`clyao8wx60024h2stw3o2ejh8`) or a new one, which decides if #400 still applies.

## 8. Smoke checks after deploy

| Check | Expect |
|---|---|
| `GET /health` | `200 {"status":"ok"}` |
| `GET /ready` | `200 {"status":"ready","checks":{"database":"ok"}}` |
| `GET /api/v1/me` with a real signed-in token | `200` |
| Any response | carries `X-Request-ID` (`src/app/api/request_id.py`, decision 241) |
| Railway logs | JSON lines with `request_id` (ADR 0030) |
| Sentry (optional) | a test event arrives. **Owner to confirm:** how to raise one; the repo has no test route |

## 9. Open launch items

| Issue | One line |
|---|---|
| #426 | Per-caller rate limits at the Vercel edge, and lock the Railway origin to a proxy secret |
| #403 | Meet guests whose account email is not their Google sign-in wait with nobody to admit them |
| #363 | Monthly credits top up to 3 minus bonus held; bonus never expires, caps at 3 |
| #9 | Branch protection on `main`. **Owner to confirm:** the repo is now public (GitHub API, 2026-10-12), which #9 named as one way to unlock it |
