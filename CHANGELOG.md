# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`.github/workflows/release.yml` parses the section matching the tag being
released. A tag with no matching section here fails the release job.

## [Unreleased]

### Added

- ADR 0002 (Bubble export strategy) and ADR 0003 (read-only freeze cutover).
- `project-conventions` filled in with the project's settled decisions, domain
  vocabulary, guardrails, and the current enforcement blind spots.
- A `references/failure-modes.md` row for the stacked-PR merge that closed a
  dependent pull request irrecoverably, and the merge order that prevents it.
- `main-guard.yml`, which fails when a commit reaches `main` without a pull
  request. It detects a bypass after the fact and cannot prevent one; server-side
  prevention needs GitHub Pro or a public repository (issue #9).
- ADR 0004 (calendar integration), 0005 (data platform) and 0006 (messaging
  build-vs-buy), plus `docs/adr/README.md` as the index, and settled decisions
  12–19 with the `port`, `ror_id` and `whatsapp_conversation_id` vocabulary.
- ADR 0007 (adopt the migration package as the target data model) and settled
  decision 20. `docs/edufurther-migration/` is committed as the canonical target
  schema — received, never edited here. It reconciles four points where the
  package and this repository disagreed: the migration anchor column is
  `legacy_bubble_id`, a booking and a session are one `sessions` row, the export
  transport is the Bubble Data API staged as raw `jsonb`, and the vendor WhatsApp
  thread identifier is renamed `whatsapp_conversation_id`. Institutions,
  first-login authentication and message-thread scope stay open and are deferred
  to ADRs 0008–0010. Adds a guardrail requiring credential fields to be redacted
  at extraction rather than at load, and records in `docs/adr/README.md` how a
  partially superseded record states its status.
- ADR 0011 (Alembic is the migration chain) and the persistence foundation:
  SQLAlchemy 2.0 with asyncpg, the async engine and session factory in
  `infra/db/`, the Alembic chain outside `src/`, a Postgres 17 compose service on
  port 55432, and database tests that skip locally without `TEST_DATABASE_URL`
  but fail in CI, where `REQUIRE_DB_TESTS=1` turns the skip into an error. The
  first migration installs `pgcrypto`, `uuid_generate_v7()` and `set_updated_at()`.
- `countries` (249 rows) and `languages` (7,078 rows), seeded from ISO 3166-1 and
  ISO 639-3 by `scripts/generate_reference_seeds.py`, which emits the migration
  rather than the migration being hand-written. Both are keyed on their natural
  ISO code. Languages use 639-3 because 639-1 omits Nigerian Pidgin entirely, and
  only 174 of the 7,078 carry a two-letter code at all.
- Settled decisions 21–25: phase-scoped enums and lookups with the foundation
  exception, natural keys on ISO lookups, the per-table `updated_at` trigger, no
  `legacy_bubble_id` on reference tables, and the Alembic chain.
- ADR 0009 (first-login authentication) — Supabase Auth for every login, a
  6-digit email code as the default with a sign-in link offered as a choice,
  delivered through a Send Email Hook that also routes authentication mail
  through Emailit. Drops `auth_codes`, `auth_code_purpose` and
  `users.password_hash` from the target schema, and makes `users.id` the Supabase
  auth user id.
- ADR 0008 (institutions) — the hipolabs registry, populated on demand and stored
  once referenced, with a surrogate `uuid` primary key and `domain` as the natural
  key. **No `ror_id` column**, which supersedes settled decision 17 in full and
  retires the `ror_id` vocabulary term — both **on acceptance**, along with
  `README.md`'s stack table. The record is `Proposed`, and the settled-decisions
  table is loaded at the start of every build, so it is not rewritten to describe
  a decision that has not been made. Same sequencing as ADR 0009.
  The record makes no deviation from the migration package, whose own D15
  had already been revised from seeding ROR to populating from hipolabs. It records
  two M2 prerequisites the M0 chain does not provide — the `pg_trgm` extension and
  the `lookup_status` enum — and that nothing monitors the hipolabs dependency the
  decision accepts.
- `fastapi[standard]` replaces the bare `fastapi` dependency, adding `jinja2`,
  `python-multipart`, `email-validator` and the `fastapi` CLI — 14 packages in
  total. The extra also pulls `fastapi-cloud-cli`, which depends on **`sentry-sdk`**,
  so an error-reporting SDK is now a runtime dependency that arrived without an
  ADR. It is listed in `[tool.check-layers.forbidden-external]` for `domain`,
  `api` and `core`, and **that entry is narrower than it sounds**: it covers those
  three layers *except the composition roots*. `main.py` and `api/deps.py` are
  exempt, and `main.py` is skipped before exemption is even consulted because it
  sits outside any layer — so the one file where `sentry_sdk.init(dsn=…)` would
  naturally be written is the one file the guard does not read. What makes this
  safe is not the denylist: `sentry_sdk` is inert without an `init()` call, nothing
  calls it, and no DSN is configured. The entry prevents an accidental import;
  initialising it deliberately would be an ADR. The guard was verified by importing
  `sentry_sdk` into `domain/` and then `core/` and watching `check_layers.py` fail
  on each before the probes were removed.
  `fastapi[standard-no-fastapi-cloud-cli]` is the upstream extra that excludes
  both, and was considered and declined: FastAPI Cloud is the deploy target and
  the CLI is used. The standing cost is that `pip-audit --strict` now covers 14
  more packages, so a future `sentry-sdk` advisory will break the weekly security
  workflow for a dependency the application never calls.
- ADR 0012 (Google OAuth scopes and client split) — both Google integrations stay
  inside the **non-sensitive** scope tier, and sign-in and calendar move to
  **separate Cloud projects**. Calendar uses `calendar.freebusy` to read
  availability and `calendar.app.created` to write events into a secondary
  calendar the application creates; the latter is a full create/change/delete
  capability and is non-sensitive, because the application can only ever touch a
  calendar it made. Sign-in keeps `openid`, `userinfo.email` and
  `userinfo.profile`. The projects are split because the consent screen — branding,
  scopes, verification status — is project-level, so a sensitive scope added for
  44 mentors would attach a user cap to 1,200 sign-in users. It supersedes ADR
  0004's "submit it for sensitive-scope verification" clause, and records that two
  behaviours it depends on are untested: whether an app-created calendar sends
  attendee invitations, and whether its busy intervals reach the mentor's own
  free/busy.
- **M2's four lookup catalogues** — `institutions`, `degree_levels`,
  `service_offerings` and `scholarship_programs` — with the `pg_trgm` extension
  and the `lookup_status` enum. Two are open, with `status`, `merged_into_id` and
  `usage_count` for the curation queue ADR 0008 and package D15 require; two are
  closed vocabularies the product defines. `institutions` ships empty by design,
  populated on demand. Only `lookup_status` is created — the other six enums in
  `02_profiles.sql` arrive with the tables that use them (decision #21). Country
  becomes `country_id uuid` rather than the package's `char(2)`, per ADR 0015, and
  the package's deferred `created_by`/`approved_by` attachments are ordinary
  inline foreign keys here because `users` already exists in our chain.
- **`service_offerings` seeded with six rows, where the package seeds none.**
  Reading the legacy option set corrected package D12's premise: Bubble held
  **one** vocabulary used by both sides, not two unmapped ones — both columns
  store the display name as text at selection time, so the mentee side is six
  parents and the mentor side five parents plus five children and renames.
  Seeding the parents is what makes matching work at all. Settled decision 52
  records why the table is closed and what re-opening it would cost.
- **`scholarship_programs` seeded with ten curated programmes**, so
  suggest-before-create has something to match against from day one; `funding_type`
  and `degree_levels` are left empty deliberately. Settled decisions 53 (model
  module layout and its split threshold) and 54 (the fail-open `status` default,
  and the obligation it puts on every write path until admin curation ships).
- Two `failure-modes.md` rows: a text snapshot of a controlled vocabulary is a log
  of past UI states rather than a vocabulary, and a migration rewritten under an
  unchanged revision id leaves every already-migrated database silently diverged.

### Changed

- **ADRs 0004 and 0009 carry correction notes: Google OAuth verification is not on
  the critical path, and never was.** Both records treated sensitive-scope review
  as unavoidable — 0004 stating that "Google Calendar scopes are in the *sensitive*
  tier" and gating calendar connect on weeks of queue time, 0009 calling
  verification "the long pole" that "gates the majority login path". Checked
  against the scope list in the Google Cloud console rather than recalled, the
  scopes both decisions actually need are **non-sensitive**: `openid`,
  `userinfo.email` and `userinfo.profile` for sign-in, and `calendar.freebusy`
  plus `calendar.app.created` for calendar — the latter granting full create,
  change and delete on secondary calendars the application makes. No app review,
  no user cap, no unverified-app warning. Neither decision changes; both are
  better served, because the scope pair is strictly narrower than either record
  contemplated. Recorded as **v1 of the Google integration** — sensitive scopes
  remain available later, with a record of their own, if a capability needs them.
  Original text left intact in both, per the convention ADRs 0002 and 0003 set.
- **ADR 0008 is accepted**, and no live assertion of ROR survives. Settled
  decision 17 becomes the hipolabs registry — populated on demand, surrogate
  `uuid` primary key, `domain` as the natural key, no `ror_id` — with the
  reasoning that the dependency lands on writes only and that Bubble already runs
  on hipolabs autocomplete. The `ror_id` vocabulary term is replaced by
  `institutions.domain`; that one mattered more than the decision row, because it
  read as an instruction to create a column the target schema does not have.
  `README.md`'s stack table is corrected to match.
- **ADR 0007's status now names its resolved deferrals** — institutions by ADR
  0008, first-login authentication by ADR 0009, with message-thread scope still
  reserved for ADR 0010. ADR 0002's status already carried its transport deferral
  this way and 0007 did not, which made two records with the same shape read
  differently. Its deferral table is deliberately left as written, including the
  stale claim that institutions block M0.
- **ADR 0009 is accepted**, and the documents that assumed a magic link now agree
  with it. Settled decision 7 describes the decided mechanism — OAuth for Google
  or LinkedIn, and on email a 6-digit code by default with a sign-in link as a
  choice — while its substance and its reopen condition are unchanged. Settled
  decision 12 no longer justifies Supabase on *magic-link* login specifically;
  passwordless email login still arrives without being built, which is what that
  argument needed. ADR 0002's status names **point 9** as superseded on the
  mechanism, restructured as a list so three qualifications stay readable, and its
  body is untouched. ADR 0004 is deliberately left alone: it cites decision 7 for
  the fact that users are already re-authenticating when the calendar reconnect
  arrives, and that holds whichever mechanism they are in.
- Target Python 3.14 across `.python-version`, `requires-python`, ruff and mypy.
- CI tests a single interpreter instead of a 3.12/3.13 matrix — this is a
  deployed application, not a library consumed on many Python versions.
- CI and the release workflow select steps from `scripts/check.py` with `--only`
  instead of restating the commands, so the gate is defined once. The bandit
  step gains `-c pyproject.toml`, which the restated CI copy had dropped.
- `[tool.check-layers.forbidden-external]` now covers `api` and `core` as well as
  `domain`, and names the adopted vendor SDKs, so "no vendor SDK outside `infra/`"
  is enforced rather than only documented. It is a denylist: a newly adopted
  vendor is unguarded until its package name is added alongside the dependency.

### Fixed

- The shared `settings` test fixture no longer reads the developer's `.env`;
  any field it did not pin explicitly was taking that file's value.
- Settled decisions 12–19, and two rows in `references/failure-modes.md`, were
  separated from their table headers by a blank line and so rendered as plain
  text rather than as table rows. Both tables are contiguous again.

## [0.1.0] - 2026-08-01

### Added

- Project skeleton: `src/app/{api,domain,infra,core}` with the layer boundary
  enforced by `scripts/check_layers.py`.
- Configuration through `core/config.py` only, rejecting misspelled
  `EDUFURTHER_` environment variables at startup.
- `GET /health` liveness endpoint.
- Full local gate via `scripts/check.py`, wrapped by `make check`.
- CI, security, and release workflows; pre-commit hooks including secret
  scanning and Conventional Commits.
