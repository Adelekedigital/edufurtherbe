# 30. Structured logs on stdout, and Sentry wherever a DSN is set

Date: 2026-10-12

## Status

Accepted.

## Context

The pre-launch audit found that nothing configured logging. Python's root logger
stayed at WARNING, so every `logger.info(..., extra=...)` the code writes (job
runs, deliveries, skipped reminders) was dropped, and errors reached stderr as
plain text. No request carried an id, so one user's failure could not be followed
through the log. Nothing reported errors: `sentry-sdk` shipped in the image only
as a dependency of `fastapi-cloud-cli`, never started, and the layer config said
starting it was an ADR because its events can carry stack traces with emails and
ids in them.

The owner decided Sentry is wanted in production now and in development later
(2026-10-12), with no code change between the two.

## Decision

1. **Logs are JSON on stdout**, rendered by structlog over the standard library
   (`infra/observability.configure_logging`, called by `create_app`). Every
   existing stdlib call keeps working; its `extra=` fields become JSON keys.
   Uvicorn's own loggers go through the same pipeline. `LOG_FORMAT` overrides the
   default, which is console text locally and in CI and JSON anywhere deployed;
   `LOG_LEVEL` defaults to INFO.
2. **Every request has an id** (`api/request_id.py`): a caller's `X-Request-ID`
   is kept when it is 8–128 safe characters, otherwise a new one is made. It is
   on every log line the request writes, on the response header (exposed through
   CORS), and on the request's Sentry events as the `request_id` tag.
3. **Sentry starts if and only if `SENTRY_DSN` is set**, never by environment
   name. It reports errors only (`SENTRY_TRACES_SAMPLE_RATE` defaults to 0),
   tagged with the environment and Railway's commit as the release. It sends no
   user, cookies, headers, request bodies or frame locals.
4. **One scrubber for both** (`observability.scrub`): email addresses and bearer
   tokens are masked inside any text, and the values of sensitive keys
   (authorization, cookie, email, password, any `*token*` or `*secret*`) are
   replaced, at any depth, in every log line and every Sentry event.
5. `sentry-sdk` is a declared dependency, and only `infra` may import it.

## Consequences

- An operator can search one request's lines by id, and support can ask a user
  for the `X-Request-ID` of a failing call.
- Turning Sentry on for development is setting one variable. Turning it off
  anywhere is clearing it.
- Scrubbing is pattern-based, so a personal value in an unusual shape (a phone
  number in free text, a name) can still reach a log line. The rule for new code
  stays: log ids, not people.
- Not covered: metrics, tracing beyond Sentry's optional sampling, the scripts
  run from GitHub workflows (which print), and a readiness endpoint, which is on
  the cutover checklist.

### Confirmation

- **Mechanical:** `tests/unit/test_observability.py` asserts that a stdlib line
  renders as JSON with its `extra=` fields and `request_id`; that addresses,
  bearer tokens, query-string credentials and sensitive keys are masked in log
  lines and Sentry events; that an unhandled 500 keeps its id in the log and the
  header; and that Sentry starts only with a DSN. Every guard was mutated and
  the suite went red.
- **Mechanical:** `production_gaps` refuses a production start with no
  `SENTRY_DSN` (`tests/unit/test_production_config.py`).
- **Mechanical:** `check_layers.py` keeps `sentry_sdk` out of `domain`, `api`
  and `core`.
- **Not mechanical:** whether a personal value in an unusual shape reaches a log
  line. Pattern scrubbing cannot prove a negative; review of new log calls is
  the check.
