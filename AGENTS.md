# AGENTS.md

EduFurther backend: FastAPI + async SQLAlchemy + PostgreSQL (Supabase), migrating
a Bubble app's data. Layers: `api/` transport, `domain/` pure rules, `infra/`
adapters, `core/` config and errors; `migrations/` is the Alembic chain.

The rules below restate `CLAUDE.md` and `.claude/skills/project-conventions/` for
a reviewer that does not load them. **Those files win on any conflict.** Every
rule here traces to a defect this repository actually shipped or nearly shipped
(`.claude/skills/project-conventions/references/failure-modes.md`).

## Code Review Rules

Flag what would break a user, leak data, corrupt a row, or break a published
contract. Judge reachability with any value the configuration or the API permits,
not only the defaults, and state the input that triggers it.

### Severity

The one definition, used by every reviewer of this repository (owner, 2026-10-09).

- **P0, critical: must fix.** Leaks data across users, forges or corrupts money,
  credits, refunds or attendance, bypasses authentication or authorization, or
  loses committed data.
- **P1, major: must fix.** Breaks a user flow or a published contract, corrupts
  a row, or fails under a value the configuration or API permits.
- **P2, minor: fix if cheap.** A real defect with a narrow trigger, or a published
  description that contradicts the code. Not required to merge.

### Do not flag

CI runs format, lint, types, the layer check, file size, bandit, pip-audit,
gitleaks and the full suite on every push. Do not comment on style, naming, import
order, docstrings, or anything those tools decide. Never suggest lowering a
threshold, adding an ignore, or raising a limit to make a check pass.

### How to write a finding

Each finding is read by the author once and acted on. Spend words only on what
changes the fix.

- Plain text. No emoji, no badges or icons of your own, no praise, no greeting
  or closing line.
- Title: the defect in one line, stated as fact ("Retry after a lowered maximum
  returns 422 instead of the stored replay"), not advice ("Consider ...").
- Body: at most three sentences — the triggering input or state, the wrong
  result, and the safe path. Name the file and symbol; do not restate the diff
  or explain code the author just wrote.
- One finding per root cause. If the same defect appears in several places,
  report it once and list the other locations.
- Only code this pull request changes, or behaviour it changes. Nothing
  speculative: if you cannot name the input that triggers it, leave it out.
- No findings at all is a valid review. Do not pad it with minor points.

### Authorization and visibility

- **Ownership is scoped in the SQL statement itself**, on reads *and* writes
  (`UPDATE`/`DELETE` included): the `user_id`/owner predicate is in the `WHERE`.
  Flag a fetch-then-compare check, or a write whose statement trusts that a
  dependency already checked. Safe path: the predicate in the statement.
- **Not-found and not-yours are the same answer** (`NotFoundError`, 404). Flag
  any path that returns 403 or a different message for another user's row; it
  lets anyone enumerate ids.
- **Soft-deleted rows are invisible.** On a table with `deleted_at`, every
  statement, including `UPDATE`s and joins, excludes deleted rows. This has been
  missed twice, each time in the one statement nobody re-checked. Safe path: the
  shared predicate or accessor, not a hand-typed copy.
- Secrets are `SecretStr` and never reach a log line, an exception message, or a
  response body. PII (emails, names, Bubble exports) never lands in git, logs,
  or error envelopes.

### Contracts that must hold on retry and under configuration

- **An `Idempotency-Key` retry replays the stored first answer.** Flag any
  validation that depends on mutable state (settings, time, another row) and
  runs *before* the replay lookup, because a retry then fails where the original
  succeeded. Safe path: resolve the replay first, then validate.
- **Every setting in `core/config.py` can take any value in its declared
  bounds.** Flag code that hard-codes a range, default, or span which a valid
  setting can undercut (e.g. an internal 3-day lookup against a configured
  1-day maximum). Safe path: derive from the setting, `min(default, setting)`.
- **A write sends only what the client sent.** Writing `NULL` for an omitted
  field overrides the server default; a PATCH must tell *omitted* from *null*.
- **Response schemas are a published contract** the frontend generates types
  from. Removing or renaming a field, or changing its type or nullability, is
  breaking; flag it unless the PR body calls it out.

### Data integrity

- **Overbooking is prevented by a database constraint**, not a
  check-then-insert. Flag any new booking path that relies on an application
  check for exclusivity or capacity.
- Times are stored UTC (`timestamptz`, never a naive `timestamp`), with the
  mentor's IANA zone in its own column. Flag `.date()` taken from a
  UTC-normalised value when the date belongs to a local calendar day.
- `trg_set_updated_at` rewrites `updated_at` on every `UPDATE`, which destroys
  a timestamp migrated from Bubble. Flag a new writer to migrated rows that does
  not hold the trigger off with `infra/db/triggers.timestamps_from_source`.
- Importers are idempotent on `legacy_bubble_id` (or the parent's foreign key
  where the table has no Bubble id of its own).

### Migrations (`migrations/`)

`alembic check` cannot see triggers, functions, `CHECK`/exclusion constraints,
partial indexes, or constraint names, and bandit and mypy do not scan this
directory, so review is the only guard here.

- **Every table has `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`**. Any
  natural or composite key the table used to carry is re-declared as `UNIQUE`,
  because otherwise duplicates silently become legal.
- **Schema changes are expand/contract across releases.** Flag a single
  migration that drops, renames, or tightens (`NOT NULL`, new constraint)
  something the currently deployed code still reads or writes.
- **Raw SQL is parameterised.** `%`/f-string formatting of values into SQL is an
  injection and quoting bug (an apostrophe in `Bachelor's Degree` broke one).
- Enum-to-enum casts go through `::text`; a direct cast fails in PostgreSQL.
- A backfill writes a value only to rows where it applies. Flag a copy onto
  every row of a column that has a server default.
- Identifiers stay within PostgreSQL's 63-character limit; longer ones are
  truncated and hashed into a name nothing in the repo contains.

### Layers

- `domain/` holds the rules and does no I/O; `api/` is transport only. Flag a
  business rule implemented in a route or a SQL string when the layer check can't
  see it, and a vendor SDK used outside `infra/`.
- **One rule, one representation.** A predicate, mapping, or constant copied
  into a second place (a `text()` SQL string counts) is a defect. Treat it as P1
  when the copy is a visibility or authorization predicate.

### Tests

- Flag a test that cannot fail for the reason it names. Examples: an
  authorization test that stops at the dependency and never reaches the store's
  own scoping; a security property asserted only on the success path; an
  assertion like `str(status).endswith("listed")`, which also accepts
  `"unlisted"`; a fixture holding one row where the test discriminates between
  several.
