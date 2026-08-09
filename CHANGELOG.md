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
  and the `lookup_status` enum. Two are open, with `status` and `merged_into_id`
  for the curation queue ADR 0008 and package D15 require; two are
  closed vocabularies the product defines. `institutions` ships empty from the
  migration and is filled by the sync below, never by a seed. Only
  `lookup_status` is created — the other six enums in
  `02_profiles.sql` arrive with the tables that use them (decision #21). Country
  becomes `country_id uuid` rather than the package's `char(2)`, per ADR 0015, and
  the package's deferred `created_by`/`approved_by` attachments are ordinary
  inline foreign keys here because `users` already exists in our chain.
- **`service_offerings` seeded with six rows, where the package seeds none.**
  Reading the legacy option set corrected package D12's premise: Bubble held
  **one** vocabulary used by both sides, not two unmapped ones — both columns
  store the display name as text at selection time, so the mentee side is six
  parents and the mentor side five parents plus five children and renames.
  Seeding the parents is what makes matching work at all. Settled decision 53
  records why the table is closed and what re-opening it would cost.
- **`scholarship_programs` seeded with ten curated programmes**, so
  suggest-before-create has something to match against from day one; `funding_type`
  and `degree_levels` are left empty deliberately. Settled decisions 54 (model
  module layout and its split threshold) and 55 (the fail-open `status` default,
  and the obligation it puts on every write path until admin curation ships).
- Two `failure-modes.md` rows: a text snapshot of a controlled vocabulary is a log
  of past UI states rather than a vocabulary, and a migration rewritten under an
  unchanged revision id leaves every already-migrated database silently diverged.
- **M2's seven profile tables** — `mentor_profiles`, `mentor_service_offerings`,
  `education_entries`, `user_awards`, `mentee_goals`, `mentee_goal_countries` and
  `mentee_goal_needs` — with five enums, and the full-text index on
  `user_profiles.about_me` that M1 deferred. Six of the seven are reshaped for
  ADR 0015: a surrogate `id` with the invariant the package's key carried
  re-declared as `UNIQUE`. **Mentor-only and mentee-only tables reference
  `mentor_profiles(user_id)` and `mentee_goals(user_id)` rather than
  `users(id)`** — the same value, but the foreign key makes it structurally
  impossible to attach a mentor-only row to a mentee, and repointing it would be
  a one-word edit that changes nothing visible.
- **`user_scholarship_experience` and `scholarship_relationship` are not
  created**, and `user_awards` gains a nullable `scholarship_program_id` instead.
  The legacy field behind that table has no option set and no values on any row,
  so there was nothing to migrate — and it overlapped `user_awards`, giving "I
  won Chevening" two legal homes. Dropping it left `scholarship_programs` with no
  consumer anywhere in the package; the link restores one, on the
  `school_name_raw` + `institution_id` pattern where the raw text is always kept.
  Settled decision 59.
- **The package's `usage_count` column is not carried**, and the two curation
  queues are ranked by `created_at` with the usage figure computed at query time.
  It was briefly present and is removed before release, so the net effect is that
  it never shipped. The package declares it, indexes it and documents it as the
  queue's approve-or-merge signal while specifying **nothing that maintains it** —
  so it would have been zero on every row forever, and the index would have
  sorted a constant. Settled decision 56 states the rule the codebase had
  demonstrated twice and written down neither time, with a `failure-modes.md` row
  for how it got as far as a merge unnoticed. ADR 0008's open-questions section
  gains a dated correction note — the record is immutable and its six decisions
  are untouched, so the original text stays and the note says what changed, which
  is the convention `docs/adr/README.md` sets for a premise overtaken by events.
- Settled decisions 57 (`requires_booking_confirmation` defaults to `false`, and
  what bounds the exposure) and 58 (legacy `meetingDuration` is an M4 input,
  becoming the duration of each auto-created "General Mentorship" session type).
  `education_entries` ships without `school_short_form` — legacy `shortForm`
  holds degree abbreviations, not school ones — and without `field_of_interest`,
  which is deprecated in the source application.
- `add_user` moves from `test_identity_schema.py` to `conftest.py`, now that a
  second schema suite needs it. A private copy that supplied its own `id` would
  keep passing after somebody removed the `uuid_generate_v7()` default.
- **`test_no_declared_identifier_exceeds_the_postgresql_limit`**, after review
  found a foreign key whose convention-generated name was 65 characters and
  which PostgreSQL therefore held under a truncated, hashed name appearing
  nowhere in the repository. SQLAlchemy shortens silently — no warning, and
  `op.f()` does not exempt it — while `alembic check` compares foreign keys by
  column signature rather than by name, so the whole gate stayed green. The name
  is shortened and the guard walks `Base.metadata`. Three further names in this
  schema sit at 58 and 59 characters, so the margin was one long table name.
- **M2's profile transform and loader** — `domain/transform/profiles.py`,
  `infra/etl/profiles.py` and `scripts/load_profiles.py`, filling all seven
  profile tables from a legacy snapshot and reconciling them. `domain/transform`
  becomes a package (`identity` + `profiles`, everything re-exported so no import
  changed), and `infra/etl/cli.py` holds what two loader scripts now share.
  Verified against the dev export end to end: 12 mentor profiles, 21 education
  entries, 13 goals, 10 awards, run twice with identical counts and no
  `updated_at` touched by the importer.
- **Settled decision 60** — a migrated row is attributed by the **user-side
  link**, never by `Creator`; `Creator` is a cross-check whose disagreements are
  reported, and the sole path only for `Scholarship-Awards`, which has no
  user-side link at all. `Creator` was re-exported as a Bubble user id rather
  than an email, which makes the comparison exact and removes the ambiguity a
  duplicate address would cause.
- **Settled decision 61** — institution matching is a separate, re-runnable
  pass; `education_entries` loads with `institution_id` null. Measured against
  real school names, a genuine typo scores **0.773** and two different Nigerian
  federal universities score **0.750**, so no similarity threshold separates
  them: exact matches auto-link and the rest are suggestions for a human.
- `EXPORT_TIMEZONE` and the canonical record's key names move to
  `domain/bubble.py`. The first was written out in two scripts and pinned by a
  test comparing exactly those two — a third would not have been covered. The
  second cost a `NOT NULL` violation on the first real load, after `modified_at`
  was hand-typed as `"updated_at"` in four places, matching the column it feeds
  rather than the key it reads.
- Three `failure-modes.md` rows: a uniform dataset can agree unanimously with a
  broken implementation (every dev date is midnight, which hid a UTC conversion
  moving evening dates forward a day); a key shared by a producer and its
  consumers belongs in the layer that defines the contract; and every test
  importing `scripts` passed only because `alembic.ini`'s `prepend_sys_path`
  had left the repository root on `sys.path` — they failed run alone. Fixed with
  an explicit `pythonpath` in the pytest config, and the suite now passes with
  random ordering enabled rather than suppressed.
- Tests for what `load_profiles.py` **surfaces**, not only what it computes.
  Unattached rows, `Creator` disagreements and nulled award years were each
  covered by a transform test, and no transform test can tell whether a value
  ever reaches a screen — the same shape as a column that reads as operational
  and is implemented by nothing. `scripts/` is checked by ruff alone.
- **The institution catalogue is mirrored and refreshed weekly** —
  `domain/institutions.py`, `infra/clients/hipolabs.py`,
  `infra/etl/institutions.py`, `scripts/sync_institutions.py` and a
  `sync-institutions.yml` workflow on a Monday cron. The mirror upserts on
  `domain` and the link fills `education_entries.institution_id` in a separate,
  re-runnable pass (decision 61). Verified against the real catalogue: 10,257
  records, 10,250 stored, and 21 of 21 education entries linked; a second sync
  refreshed every `last_synced_at` and moved zero `updated_at`.
- **`institutions.last_synced_at`, and `institutions.country_id` becomes
  nullable.** `last_synced_at` is stamped on every row a sync saw, including
  unchanged ones, so `max(last_synced_at)` answers how stale the mirror is —
  which is the objection ADR 0008 raised against mirroring at all. `country_id`
  goes nullable so a user-created row can be completed by an admin at review,
  rather than the user being asked for a field the review process exists to
  supply.
- **ADR 0020 and settled decision 62** — the catalogue is fetched over **HTTPS**
  from the source repository and the hipolabs **HTTP API is never called**. Its
  API has no TLS, so a browser on an HTTPS page cannot reach it: 0008's
  client-side autocomplete is unbuildable rather than merely deferred, while the
  same data *is* served securely from the repository. Weekly is sized to the
  measured upstream cadence — 100 commits over 177 days, one change every ~2
  days. GitHub Actions holds the schedule because `migrate.yml` already reaches
  the database from a runner, and Actions moves with the repository rather than
  the host, so decision #13's Railway exit is untouched. **Staging only** — there
  is no production environment during the build and migration phase, and a
  schedule pointed at one that does not exist fails every week quietly enough
  that nobody looks.
- **A domain two records share is collapsed before the write, not by
  `ON CONFLICT` afterwards.** Absorbed by the conflict clause, the second record
  rewrote the first on *every* sync forever — `updated_at` moving on two rows
  whose stored content never changed, which is precisely what `last_synced_at`
  was added to say. The test that claimed to cover this asserted it for a domain
  appearing once, so it could not see the case that was broken.
- **The sync workflow fails closed.** Its exit-code branch named 1 and 2 and sent
  everything else to the implicit 0 of a false branch, so an OOM kill (137) or a
  missing interpreter (127) rendered as a green weekly check. Only 2 is now
  forgiven. This matters more than it looks: nothing alerts on a sync that has
  stopped, so the weekly check is the only signal there is.
- Two records that are skipped rather than guessed, both reported by count: an
  unresolvable country code (measured at 5 of 10,257, all `XK` — Kosovo, a
  user-assigned code outside ISO 3166-1) and a record with no domain (0 today).
  Neither is defaulted; a wrong country propagates into "who studied in the UK"
  and nothing would surface it. Two upstream domains carry two names each and
  collapse to one row, counted so the total is not read as a loss.
- **A fresh environment has an empty catalogue until the sync runs** — a
  deployment step, recorded in ADR 0020 rather than discovered by whoever
  provisions the next environment.
- **A name the catalogue carries twice is a question, not a match.** Upstream
  holds 73 exactly-duplicated names over 158 records, and they cross borders —
  `City University` is a university in the United States, in Bangladesh and in
  the United Kingdom. The first implementation kept whichever row a `SELECT`
  without `ORDER BY` returned last, so the link was a coin toss that could
  change between runs, and the country of study derives from it. Ambiguous names
  now report separately from unmatched ones, because a miss wants the
  institution added and an ambiguity wants somebody to say which.
- **A sync with nothing usable exits non-zero.** Renaming one upstream key
  refused every record, mirrored nothing, printed the refusals and exited **0** —
  including the dry run CI runs first, so the weekly check stayed green over a
  catalogue that had quietly stopped updating. `HipolabsCatalogue.fetch` already
  refused an empty source; this is the same rule one layer up, where the
  emptiness arrives from refusals instead.
- Tests for the catalogue client itself, and a `failure-modes.md` row for what
  they missed. The module shipped at **0% coverage** beside a 100%-covered
  sibling, passing only because the threshold is a global 85%. Its first
  version of *"no request ever goes over plain HTTP"* asserted that on the
  **success** path, and a mutation reintroducing an `http://` fallback in the
  `except` branch left every test green — a fallback that would have worked, and
  sent users' traffic unencrypted. The assertion is now on the failure path.

- **M2's read surface** — `GET /institutions?q=`, `GET /catalog/{name}` for the
  five lookup lists, `GET /users/{id}/education|goals|awards|mentor-profile`, and
  `GET /me` extended to embed all four so a profile page renders in one call. The
  sub-resources are addressed by user id rather than `/me/...` because a platform
  admin needs one user's education; `/me` calls the same store functions and the
  same response models, with a test asserting the two payloads are identical.
- **Institution search is tiered, and that is measured rather than chosen.**
  A single query combining the tiers with `OR` defeats the planner — `La` costs
  36.5 ms by `ILIKE`, 9.4 ms by trigram, and **123.6 ms** combined. Run
  separately, cheapest first, stopping as soon as the page is full: prefix,
  then substring, then a fuzzy pass at a 0.5 similarity floor. A typical
  keystroke costs about 6 ms and never reaches the fuzzy tier. Typo tolerance
  works when the term resembles the whole name (`Univerity of Lagos`); it does
  **not** rescue a short misspelled word against a long one (`Oxfrod`), which is
  stated in the route description and pinned by a test so the claim cannot
  quietly become false.
- **`ix_institutions_name_prefix`** on `lower(name) text_pattern_ops`. The GIN
  trigram index does not serve a prefix match, and prefix is autocomplete's
  common path — one query per keystroke — at 59.3 ms without the index and
  4.3 ms with it. `alembic check` **cannot** compare an expression index with an
  operator class (it says so and skips), so two tests cover what the gate
  cannot: the index is declared on the model, and present in the database.
- **`languages` holds 7,078 rows**, so the lookup lists take `?q=` and page by
  keyset rather than returning everything. The obvious trim — restrict to the
  174 ISO 639-1 codes — is ruled out by the schema's own reasoning: 639-3 was
  chosen precisely because the two-letter set omits Nigerian Pidgin. Closed
  vocabularies keep their `sort_order` and never page, so degree levels still
  read "Undergraduate, Diploma, Masters" rather than alphabetically.
- **Every unhandled exception is now Problem Details too.** Only deliberate
  `AppError`s were registered, so anything else — a database error, a bug, a
  third-party failure — left as Starlette's `text/plain` "Internal Server
  Error". That breaks ADR 0016's promise precisely in a client's error path,
  where a JSON parse failure turns one fault into two. Found by pointing the
  application at an unmigrated database. The detail is withheld (a database
  error names tables and columns) and the traceback is logged, because a 500
  that tells the caller nothing *and* the operator nothing is worse than the
  plain text it replaces.
- **`LIKE` wildcards in a search term are escaped.** The term is bound as a
  parameter, which stops injection and does nothing about the *pattern* being
  user-controlled: measured, `q=%` matched every institution, `q=____________`
  matched every name of twelve characters or more, and `q=100%` matched nothing
  where it should find "100% Academy". On a public unauthenticated endpoint that
  is also a one-character way to make every tier match everything.
- **ADR 0016's cursor rule is amended, scoped rather than reversed** — the id is
  the cursor for a list displayed in insertion order; a list displayed in some
  other order keys on its sort column plus the id. An id cursor over 7,078
  alphabetically-rendered languages pages by creation time and silently skips
  rows. The envelope, the cursor's opacity and "every list endpoint" are
  untouched.
- A `catalog` tag description, and the `users` one corrected: that group now
  serves `/users/{id}/...` for admins, not only the caller's own record.
- **`list_awards` returned soft-deleted awards**, so a user who removed one still
  saw it and so did an admin reading them. `ix_user_awards_user` is declared
  `WHERE deleted_at IS NULL`, so the query could not use the index built for it —
  a sequential scan where an index scan was intended. Found in review.
  `test_profile_store_soft_deletes` now takes the list of tables needing the
  predicate from `Base.metadata` rather than from anything a person maintains,
  so a new soft-deletable table fails until it is covered or exempted out loud.
  This is the second time this rule has been missed here — the first cost five
  statements on `users`, and the module-walk that fixed it does not transfer,
  because `profile_store` builds its statements inside functions.
- **Settled decisions 63, 64 and 65** — `status` filters search but never an
  entity read; catalogue reads are unauthenticated and tagged `catalog`; a merge
  repoints `education_entries.institution_id` rather than being resolved at read
  time.
- Two `failure-modes.md` rows, both found by mutation: four authorization tests
  that all stopped at the dependency, leaving the store's own `user_id` filter
  unexercised — dropping it would have returned every user's degrees to a caller
  entitled to their own; and a mutation harness that scored "no tests ran" as a
  kill, silently certifying untested code.

- **M2's write surface** — `POST/PATCH/DELETE` for education and awards,
  `PUT/DELETE` for the goal, `POST/PATCH` for a mentor profile, and `PATCH` for
  the user profile. **Owner only**: a live admin reads these records and never
  writes them, which is one clause of difference in the dependency and a test on
  every endpoint.
- **An unlisted school is created inside the education write's transaction**,
  `source='manual'`, `status='pending_review'`, `created_by` from the
  authenticated caller — so nothing anonymous reaches `institutions`, and a
  failure leaves neither the institution nor the entry. A name the catalogue
  carries twice is **queued, never linked**: `City University` is a real
  university in three countries and the study country derives from the choice.
  A new `CHECK (source <> 'manual' OR created_by IS NOT NULL)` enforces the
  invariant the review queue depends on.
- **Applying to be a mentor flips `primary_role`** so the applicant lands on the
  right dashboard. It grants nothing — `primary_role` picks a dashboard and is
  never an authorization claim; what a pending mentor may do follows from
  `approval_status`. **Nothing approves an application yet**, so these join
  institutions awaiting review in a queue with no owner.
- **`degree_levels` is now ISCED-aligned** — Certificate/Diploma, Bachelor's,
  Master's, Doctorate. The original six mixed education *levels* with a specific
  qualification (`MBA`, which is a master's degree) and a career stage
  (`Postdoctoral`, which is a research post — nothing is awarded), so a filter
  could not answer "mentors with a doctorate". Rows are repointed before the
  merged levels are deleted and `degree_category` keeps the raw legacy string,
  so nothing is lost. Done now because the only data in flight is test data.
- **The goal endpoints are singular.** `mentee_goals.user_id` is unique and the
  model says "1:1 with the user", so `GET/PUT/DELETE /users/{id}/goal` replaces
  a `Page` that could only ever hold zero or one. Found by a write test hitting
  the constraint on a second insert.
- **Lookup search is ranked, not alphabetical.** Twenty of the 7,078 ISO 639-3
  names contain "English", so searching for it returned Antigua and Barbuda
  Creole English first and the language the user meant fifth. Exact, then
  prefix, then anywhere — the tiering institution search already uses.
- **Every 422 is Problem Details.** FastAPI answers `RequestValidationError`
  itself with `{"detail": [...]}`, so the promise broke on the most ordinary
  failure there is: a form with a bad field. Nothing had exposed it, because the
  only 422 before writes came from our own error type.
- `PATCH` takes its own partial models. Reusing the create model made
  `school_name_raw` required on every edit, so a one-field change silently
  changed nothing.
- Inserts write **only the keys the client sent**. `payload.get(column)` over a
  column list wrote explicit `NULL`s, which override server defaults — applying
  to be a mentor without sending `requires_booking_confirmation` raised a
  not-null violation.
- **Settled decisions 66-69** and three `failure-modes.md` rows, including the
  one where a migration suppressed ruff's `S608` to build SQL by string
  formatting and was then broken by the apostrophe in "Bachelor's Degree".

- **The review surface** — `/api/v1/admin`, tagged `admin`: the pending
  institution queue ranked by how many education entries reference each row,
  approve, merge, the pending mentor queue, and a decision endpoint. **The only
  endpoints that show one user another's records by design**, so the control is
  the caller's grant rather than a row scope, and every one is tested against
  four cases: no token, no grant, a revoked grant, a live one.
- **Grants are role-specific.** `AdminRole` already distinguished
  `super_admin`, `mentor_approval` and `limited_access`; treating them as
  interchangeable would have made the enum decorative. `mentor_approval` decides
  applications and cannot curate the catalogue; `limited_access` reads and
  changes nothing.
- **Merging repoints, in one transaction** (settled decision 65). The losing
  row's entries move to the winner and it is marked `merged` together, so the
  chain collapses at merge time and no later read follows it. Merging into a
  row that is itself merged, or into itself, is a 409.
- **`declined_at` and `declined_by`.** `approved_by` had no counterpart, so a
  decline recorded *that* it happened and never *who* — and unlike a count that
  cannot be reconstructed afterwards. Reusing the approve pair would have put a
  decliner in a column named for approval, the shape `usage_count` and the old
  `updated_at` both failed in.
- **`languages.is_common`** marks the 100 languages in CLDR's `modern` coverage
  tier, with `GET /catalog/languages?common=true` serving the picker and search
  still reaching all 7,078. Replacing the table with the common set was measured
  and rejected: it would delete Efik, Ibibio, Tiv, Kanuri, Idoma, Urhobo, Nupe,
  Gbagyi, Esan, Ebira and Jukun — every one a Nigerian language, on a platform
  for African students. `pcm` is out of the default by our decision rather than
  the standard's, and stays searchable.
- `scripts/derive_common_languages.py` regenerates that list from CLDR, and a
  test asserts the seed still matches it. The migration holds a literal because
  a migration that fetches cannot be replayed.
- **`PUT /users/{id}/languages`**, carried over from the previous release's
  checklist. Replaces the whole list; at most one primary and no duplicates,
  both refused at the boundary with a 422 rather than surfacing a unique-index
  violation as a 500.
- **Settled decisions 70-72** and four `failure-modes.md` rows — including a test
  asserting `endswith("listed")`, which `"unlisted"` satisfies, and a seed
  literal typed by hand instead of pasted from its generator.

- **`mentor_status_events`, and the log becomes the write path.** Every approval
  and listing transition is recorded with who made it, when and why;
  `trg_apply_mentor_status` projects each event onto `mentor_profiles`, so
  application code inserts an event and never updates a status column. The two
  columns stay because `ix_mentor_profiles_searchable` indexes exactly them.
- **Seven columns go** — `approved_at`, `approved_by`, `declined_at`,
  `declined_by`, `decline_reason`, `unlisted_at`, `unlisted_reason`. Each held
  only the most recent decision, so a mentor declined, re-applied, approved and
  then unlisted had lost three of four. `ix_mentor_profiles_unlisted` goes with
  them and is not replaced: the query it served now runs against the log.
- **An admin can unlist an approved mentor without declining them** —
  `POST /admin/mentors/{id}/listing` — which is the third transition that made
  two columns per state stop scaling. `GET /admin/mentors/{id}/history` reads it
  back, newest first.
- **A mentor can pause and resume their own listing**, and **only their own**:
  resume is refused unless the newest unlisting carries `mentor_paused` and the
  mentor is approved. Otherwise a suspension would be a button the suspended
  person can press.
- **`scripts/check.py --fast`** — ruff, mypy, layers and unit tests, **~20
  seconds against 8–15 minutes** for the full gate. The suite is ~95% of the
  gate's runtime, so a one-line lint error cost a full cycle to find; three
  commits in the previous releases were rejected for something ruff answers in
  two seconds. It skips the database tests and coverage deliberately, and is not
  a substitute for the gate before committing.
- **Settled decisions 73-75**, four `failure-modes.md` rows, and one more entry
  on the `alembic check` blind-spot list — it cannot see triggers, and one now
  carries a rule.

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
