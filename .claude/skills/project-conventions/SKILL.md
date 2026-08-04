---
name: project-conventions
description: This project's own settled decisions, house conventions, domain vocabulary, and guardrails — the things that are true here and nowhere else. Use at the start of any build in this repository, when choosing between two valid approaches, when a generic standard seems to conflict with how this codebase does it, before proposing a pattern the codebase has not used before, or when a requirement uses a term with a project-specific meaning.
---

# Project Conventions — EduFurther Backend

Tier 2 of the standards. The generic skills alongside this one are
project-agnostic and get overwritten on update; **this file is ours** and is
never overwritten.

## What this project is

Backend for a mentorship platform connecting African students pursuing
international graduate school with mentors at institutions worldwide. It is
replacing a Bubble low-code application, with a separate Next.js frontend, while
adding paid mentor sessions.

Two facts drive most decisions: **the data is small** (1,200 users, of whom 44
are mentors; 1,073 session bookings) and **the migration is a reshape, not a
copy**. Small data means correctness is affordable and throughput machinery is
not worth building. A reshape means the Bubble schema is an input to a mapping,
never a template.

Figures come from `docs/bubble-data-model.md`, which is canonical. Do not quote
the numbers on the public site — they are marketing figures and understate the
database by roughly 3x on mentors.

## Settled decisions

| # | Decision | Why | Reopen if |
|---|---|---|---|
| 1 | Python 3.14 only. No CI interpreter matrix | This is a deployed application, not a library consumed on many interpreters. A wider `requires-python` than CI exercises is an unverified compatibility claim | We ever ship this as a library, or a dependency blocks 3.14 |
| 2 | The package is `src/app/`, not `src/edufurther/` | FastAPI's own conventions use `app/`. The `src/` layer forces tests to import the *installed* package, so packaging mistakes fail in CI rather than at deploy | Never, absent a real import collision |
| 3 | `scripts/check.py` is the single definition of the local gate; `make check` is a thin wrapper | `make` is absent on Windows dev machines. Two hand-maintained copies of a gate drift, and the forgotten copy is the one that stops catching things | `make` becomes universally available here |
| 4 | ruff, mypy and bandit are pinned with `==` and kept equal to the `rev:` values in `.pre-commit-config.yaml` | They decide whether code passes, and their verdicts change between versions. A floor range let the venv drift a full major version from the hooks | Never. If it is painful, the fix is updating both sites together |
| 5 | Cut over behind a read-only freeze. No dual-run, no dual-write (ADR 0003) | A sync layer is the most defect-prone part of a migration, and here the schemas deliberately differ so it would have to maintain a bidirectional transform between shapes that never corresponded | Revenue or usage grows enough that a scheduled write outage is unacceptable |
| 6 | Bubble export is snapshot-first. The transport is the **Bubble Data API**, staged as raw `jsonb` in a `staging` schema, and still sits behind a port (ADR 0002, deferral resolved by ADR 0007) | The freeze is short and Bubble goes away after; a transform bug must be re-runnable against the original bytes rather than a rate-limited API. The Data API is subject to privacy rules, so per-Thing field-set verification is **mandatory** — a 200 response is not evidence of a complete record | Never for snapshot-first. The transport, only if the Data API proves lossy |
| 7 | Identity does not migrate. Accounts match on email; every user does a magic-link or reset on first login | Bubble password hashes are not exportable. This is a constraint, not a preference | Bubble ever exposes usable hashes |
| 8 | **Payments are out of scope for the initial build.** It delivers the core product backend and the Bubble migration; the legacy app has no payment integration to migrate | Nothing to carry across, and the pricing model is still being explored. Building a payment layer around an undecided model is how you get one you cannot change | The core backend and migration are done |
| 9 | A mentor sets their own **rate**; the platform offers a derived **pricing guide** as a suggestion | Mentors price their own time. The guide helps them choose without the platform setting the price | The platform ever takes pricing control |
| 10 | When payments arrive: money as integer minor units with an explicit currency; mentor payouts through an append-only **ledger**, paid by hand at first; the rate **snapshotted onto the purchase**, never read live from the profile | Floats do not represent money, and cross-currency is structural here. A ledger cannot be reconstructed after the fact. A live rate lookup means a mentor raising their price silently rewrites history | Never for the first two. The third only if rates become immutable |
| 11 | Payment provider: **undecided**, and not yet needed | Collections are African (Paystack/Flutterwave territory), payouts are global (Stripe/Wise territory). The two halves may not share a provider | — decide before payments work begins; it needs its own ADR |
| 12 | **Supabase** for Postgres, auth and storage, this phase. Used *as Postgres* — no PostgREST, no RLS as application logic (ADR 0005) | One vendor and one credential set during the cutover, and magic-link login arrives without being built, which decision #7 needs for all 1,200 users. A policy in the database is invisible to `check_layers.py` and unreachable by a unit test, so authorization expressed there drifts from the Python that appears to implement it | Auth or storage needs to diverge from the database, or one shared failure domain across all three stops being acceptable |
| 13 | **FastAPI Cloud**, with Railway as the named fallback | Chosen deliberately. The escape stays real only by keeping a standalone Dockerfile exercised in CI and using no platform-native queue, cron or secrets store — an untested exit is an intention, not a plan | The platform cannot carry the freeze rehearsal, or a platform-native primitive becomes tempting |
| 14 | LLM access goes through a **provider-agnostic port**. No gateway | A port gives model swappability without a gateway's fee, extra hop, and flattening to the intersection of every provider. Adapters may use each provider's own caching, structured outputs and thinking configuration; the port must not degrade to the lowest common denominator | Never for the port. Which model is default is a config change, not a decision |
| 15 | Calendar is a **write target and an on-demand free/busy read** — never polled, never mirrored (ADR 0004) | Polling costs orders of magnitude more and is staler than reading at the moment of decision. Only mentors connect; mentees receive a calendar invitation and complete no OAuth flow | A proactive "your mentor's calendar now conflicts" feature is required, which on-demand reads cannot serve. Price the polling then |
| 16 | PWA push is **native Web Push** — VAPID keys, a service worker, no vendor | The browser does this. A notification platform earns its place at multi-channel fan-out with a preference centre, which is a later problem. iOS delivers push only to installed PWAs, so push is never the sole channel for anything that matters | A preference centre becomes a real requirement — then Novu, self-hosted |
| 17 | Institutions are the **hipolabs registry, populated on demand** (ADR 0008). Autocomplete is served live; a row is stored only once someone selects it, upserted on `domain`. Surrogate `uuid` PK, `domain` as the natural key, **no `ror_id`** | Storing what is referenced gives ~200–400 rows rather than ~9,000, and the dependency lands on writes only — reading existing education history never touches hipolabs, which is what the earlier ROR version of this row was trying to buy. Bubble already runs on hipolabs autocomplete, so this is the incumbent dependency rather than a new one. `school_name_raw` is always kept, so an unmatched institution degrades display rather than losing data | Institution identity becomes a cross-system matching problem — mergers or renames breaking joins, or a partner exchanging institution records. Then ROR returns, with a backfill |
| 18 | WhatsApp is **platform→user transactional only**. Mentor↔mentee conversation stays in-platform (ADR 0006) | Handing both sides of a paid booking a direct off-platform channel is disintermediation by default rather than by decision | Never, while the platform takes a fee |
| 19 | ADRs use **Nygard** format, per ADR 0001 — *not* MADR | `/adr-new` ships with the tier-1 package and refers to the MADR template. Tier 1 is overwritten on update, so correcting the command would not survive; tier 2 does. Nygard is also the lower-ceremony format, and 0002 and 0003 already carry considered-options reasoning inside it without strain | ADR 0001 is superseded — which is itself an ADR |
| 20 | `docs/edufurther-migration/` is the **canonical target data model** — received, committed, and never edited here (ADR 0007) | It is the only artefact describing the target schema rather than the route to it, and part of its value is being a dated record from outside this repo. A transcribed copy would be a second copy that drifts from the DDL you actually run | A decision supersedes part of it — which is a new ADR, never an edit to the package |
| 21 | **Nothing ships earlier than the phase that creates a dependency on it.** In practice enums and lookups arrive with the phase that first uses them — with one standing exception: a foundation phase may carry a lookup whose rows the *next* phase's foreign keys require. `countries` and `languages` ship in M0 for exactly that reason; `institutions`, `degree_levels`, `service_offerings` and `scholarship_programs` wait for M2, and `legal_documents` for M1 | `00_foundation.sql` declares 40 enums and 7 lookup tables; exactly 2 of those enums are used by its own tables. Several encode business decisions this project has explicitly *not* made — `credit_source` contains `purchase` while payments are out of scope by decision #8 — and a shipped enum is a schema asserting a choice nobody took. ADR 0011 point 6 states the same rule in its shorter form, written before M0 was built; that record is accepted and immutable, so this row is where the exception lives | A phase genuinely needs a type before the table that constrains it |
| 22 | **ISO lookup tables are keyed on their natural key**, not a surrogate `id` — `countries.code`, `languages.code_639_3`. This overrides `persistence-patterns`, which requires a surrogate on every table | The ISO code is exactly what every foreign key stores. A surrogate would mean each FK holds a UUID that must be joined back to recover the code the caller already had. The generic rule buys uniformity; it stops paying when the key is externally standardised, stable, and already the thing being referenced | A lookup's key stops being externally governed, or starts changing |
| 23 | **The `updated_at` trigger is attached per table, in the migration that creates it** | The handoff used a blanket function that scanned and re-triggered every table in the schema. That is non-deterministic across environments, invisible in a diff, and on Supabase `public` is not exclusively ours. Explicit costs one line per table and can be forgotten — which is why a test asserts every table carrying `updated_at` actually has the trigger | Never. If the per-table line is tiresome, the answer is the test, not a scanner |
| 24 | **Reference tables carry no `legacy_bubble_id`** | The guardrail applies to *migrated* tables. `countries` and `languages` are seeded from a published standard, not carried across from Bubble, and a column null on all 7,327 rows is not a migration anchor | A reference table ever does originate in Bubble |
| 25 | **Alembic is the migration chain; the package DDL is a specification, never executed** (ADR 0011). SQLAlchemy 2.0 + asyncpg; models are the source of truth for tables and columns, everything autogenerate cannot see is hand-written | Ten `.sql` files that run against an empty database describe a destination; a chain has to move a *populated* database. The moment production holds a row those files can never run again, so adopting them would mean a chain with one link. Models also make `alembic check` and the every-model timestamp test possible, and both are guardrails this project already claims | Never for the chain. Whether a given migration is autogenerated or hand-written is a per-migration judgement |

Anything conflicting with a row here is an **ADR**, not an implementation detail.

## Domain vocabulary

> **Two rows are still open, and the gaps are load-bearing.** If a requirement uses
> one of those words, **ask**. Do not resolve it toward whichever reading is easier
> to build.

| Term | Means precisely | Does **not** mean |
|---|---|---|
| **Mentee** | The student receiving mentorship | Not "user" — mentors are users too |
| **Mentor** | The person providing mentorship, typically at an institution abroad | **There is no separate "coach".** "Mentor/coach" in product copy is one role, and the codebase uses `mentor` throughout |
| **Availability** | A mentor-declared window in which they *can* be booked | Not a session. An availability with no booking is not an event that happened |
| **Booking** | The *act* of a mentee claiming a slot. It creates a `sessions` row (ADR 0007) | **Not a separate entity.** There is no `bookings` table — the legacy `SessionBooking`/`SessionTracker` split was a Bubble workaround, not a domain distinction |
| **Session** | The `sessions` row itself, from the moment it is claimed through whatever it becomes | **Not "mentorship that took place"** — that is `status = 'completed'`. A cancelled session is still a session row, still counted, and is never soft-deleted |
| **Rate** | What a mentor charges, set by the mentor | Not the pricing guide |
| **Pricing guide** | A platform-computed *suggestion* derived from the mentor's experience and profile | **Not a price.** Advisory only, never persisted as the agreed amount |
| **`legacy_bubble_id`** | The Bubble `_id` a row came from. ADR 0002 called it `legacy_id`; renamed by ADR 0007 | Not our primary key, and never exposed in an API |
| **Port** | An interface `domain/` owns — a Protocol in `domain/ports.py` — implemented by an adapter in `infra/` and wired in `api/deps.py`. Swapping a vendor is a new adapter class and one wiring line | Not a network port. Not a thin wrapper around a vendor SDK either — if the vendor's vocabulary shows through the Protocol, `domain/` is still coupled to it |
| **`institutions.domain`** | The hipolabs domain for an institution — the natural key rows are upserted and deduplicated on, and what "which university" resolves to. Null only for `source='manual'` (ADR 0008) | Not the primary key — that is the surrogate `uuid` another row points at. Not a university name either: a name is a display string. **There is no `ror_id`**; ADR 0008 supersedes the settled decision that defined one |
| **`whatsapp_conversation_id`** | Zernio's WhatsApp thread identifier, stable per participant and sending account, stored on the user record. Renamed by ADR 0007 — a vendor identifier carries the vendor's name | Not a message id. Unrelated to our own `conversation_id`, which is the primary key of our booking-scoped message threads (ADR 0006) and lives in our database |

**Session shape.** Sessions may be 1:1 **or group**. Group is a new capability
that does not exist in the legacy app — **every legacy record is 1:1**, so the
migration never encounters a group session and the importer does not need to
handle one. When group is built, the booking invariant is **capacity, not
exclusivity**, so it cannot be a plain uniqueness constraint on the slot.

**Still open — do not guess:**

- **Does a mentee pay per session, per package, or by subscription?** Deliberately
  undecided while the model is explored. Do not hard-code any one of them. The
  shape that keeps all three open is to separate what was *purchased* from what
  was *booked*, so a new model is a new purchase type rather than a migration.
- **What is the cancellation and refund window, and who absorbs the fee?**
  Deliberately undecided. Express it as a parameterised policy in `domain/`, not
  as constants in a handler, so changing it changes parameters.

Neither blocks the initial build: **payments are out of scope until the core
product backend and the migration are done.**

## House conventions

- **Configuration:** everything through `core/config.py`, `EDUFURTHER_`-prefixed.
  No inline `os.environ` anywhere else. Unrecognised prefixed variables fail at
  startup — `extra="forbid"` does **not** do this on its own, which is why there
  is an explicit validator.
- **Secrets:** `SecretStr`, never logged, never in a response body. In a committed
  `.mcp.json` or similar, referenced as `${ENV_VAR}` and never inline.
- **Identifiers:** three distinct spaces, never interchangeable — our internal
  primary key, the `legacy_bubble_id` from Bubble, and any provider-side id (payment
  intent, payout). Translate at the boundary; do not compare across spaces.
- **Money** *(when payments arrive — not in the initial build)*: integer minor
  units plus currency, never a float. The ledger is append-only; corrections are
  new entries, never `UPDATE`s.
- **Vendor SDKs:** may be imported only inside `infra/`. `domain/` expresses the
  need as a Protocol in `domain/ports.py`.
- **Errors:** subclasses of `core.errors.AppError`, which is transport-agnostic.
  HTTP status codes are chosen in `api/`, never raised from `domain/`.
- **Not-found vs not-yours:** both raise `NotFoundError`. Distinguishing them
  leaks the existence of other people's rows to anyone who can enumerate ids.
- **Error envelope and pagination:** *not yet defined.* Decide both before the
  second endpoint exists, not the tenth — the Next.js client will encode whatever
  shape ships first.

## Guardrails

Every build preserves these, whatever it is doing. They become the "guardrails"
section of each Definition of Done.

- [ ] Object-level authorization is scoped **in the query**, on every read *and*
      write path — never checked after fetching
- [ ] No vendor SDK imported outside `infra/`
- [ ] Secrets never reach a log line, a response body, or git
- [ ] Bubble snapshots stay out of git — `data/`, `exports/`, `*.csv` are ignored
      because they hold 1,200 users' PII, plus 858 personal-info and 940
      education rows, which `gitleaks` does not scan for. It finds credentials,
      not people. **The extract now lands in a `staging` schema rather than in
      files** (ADR 0007), so the same data sits inside the Supabase project from
      ADR 0005 — out of git is still necessary and no longer sufficient
- [ ] **Credential fields are redacted at extraction, before the insert into
      `staging`** — `calAccessToken`, `calRefreshToken` and their expiry columns
      are live OAuth credentials sitting on the legacy `User` table. They are
      never migrated, but "extract everything, then transform" (ADR 0007) lands
      them in the database first and leaves them there through every rehearsal.
      Dropping them at load is too late; the exposure is the staging row
- [ ] Every migrated table has `legacy_bubble_id`; importers are idempotent on it
- [ ] Overbooking is prevented by a **database constraint**, not an
      application-level check-then-insert. While every session is 1:1 a uniqueness
      constraint suffices; group sessions make the invariant capacity rather than
      exclusivity, and the constraint has to change with it
- [ ] Times stored UTC, with the mentor's IANA zone as a separate column
- [ ] *(once payments exist)* Money is integer minor units with a currency; no
      float touches an amount
- [ ] No threshold lowered to go green

## Commands

```bash
uv sync --all-extras --dev                              # install
uv run pre-commit install                               # hooks (pre-commit + commit-msg)
cp .env.example .env                                    # local settings
docker compose up -d                                    # Postgres 17 on 55432
uv run alembic upgrade head                             # migrations (never on startup)
uv run uvicorn app.main:app --reload --app-dir src      # run
uv run pytest                                           # tests
make check                                              # the full local gate
uv run python scripts/check.py                          # the same gate, without make
```

Tests marked `db` read `TEST_DATABASE_URL` and **skip** without it, so the suite
runs on a machine with no Docker. CI sets `REQUIRE_DB_TESTS=1`, which turns that
skip into a failure.

## Where the truth lives

| Doc | Covers | Canonical copy |
|---|---|---|
| `CLAUDE.md` | Router; the few always-true facts | repo root |
| This file | Settled decisions, vocabulary, guardrails | `.claude/skills/project-conventions/` |
| `docs/adr/` | Decisions and their rationale | repo root |
| `references/failure-modes.md` | What has actually gone wrong here | alongside this file |
| `docs/bubble-data-model.md` | Legacy Bubble shape, fields, and row counts | repo root |
| `docs/edufurther-migration/` | The **target** schema, field mapping and runbook | repo root — **received, never edited here** (ADR 0007) |
| `README.md` | Human-facing setup and layout | repo root |

**Canonical copy rule.** Where a doc is duplicated, **name the one that wins** and
edit only that. Duplicated facts drift, and the copy you forget becomes wrong.

## Enforcement blind spots

State a gate's blind spots next to its coverage, every time.

- **Nothing server-side *prevents* a bad merge; one thing *detects* it.** CI runs
  on pull requests, `no-commit-to-branch` blocks local commits to `main`, and
  `main-guard.yml` fails when a commit reaches `main` without an associated pull
  request. That last one runs *after* the push has landed — it turns a silent
  bypass into a red check, and it cannot stop anything. Treating detection as
  equivalent to prevention is how a gap gets closed on paper and stays open in
  fact. Real prevention needs a GitHub ruleset, and both the classic
  branch-protection and rulesets APIs return 403 on a private repo on the Free
  plan — rulesets are not a free workaround. The choice between GitHub Pro and
  making the repo public is issue #9. There is still no CODEOWNERS file despite
  `.pre-commit-config.yaml` citing one.
- **There is no dataflow analysis.** CodeQL is absent from the Security workflow
  because uploading results needs GitHub Advanced Security, which a private
  personal repo does not have. bandit, ruff's `S` ruleset and pip-audit cover
  pattern-matched issues and known CVEs; **nothing tracks tainted input across
  function boundaries.** Restore by enabling GHAS or making the repo public.
- **`gitleaks` finds credentials, not PII.** It will not stop a member export.
- **The layer check reads imports, not behaviour.** It cannot see a domain rule
  implemented in `api/`.
- **`alembic check` sees tables, columns, types and regular indexes — and nothing
  else.** It is blind to functions, triggers, partial indexes, `CHECK` and
  exclusion constraints, and to any model never imported into `env.py`. That is
  most of what makes this schema correct: the double-booking exclusion
  constraint, the soft-delete partial unique indexes, `set_updated_at`. A green
  migration check means the chain applies, not that it is complete.
- **Nothing compares the Alembic chain against `docs/edufurther-migration/`.**
  ADR 0007 names this and ADR 0011 does not fix it. The package is canonical for
  what the target *should* contain, the chain for what a database *does*, and
  when they drift nothing says so. Mitigated only by the rule that a migration
  departing from the package states the departure in the migration.
- **Neither bandit nor mypy sees `migrations/`.** `scripts/check.py` runs
  `bandit -r src` and `mypy src`, and the pre-commit mypy hook is scoped
  `files: ^src/`. Migrations live at the repo root — deliberately, so they are
  not packaged into the wheel — which means the hand-written raw SQL most likely
  to carry an injection or a typo is checked by ruff alone. `alembic.ini`'s
  post-write hooks keep it formatted and linted; nothing type-checks or
  security-scans it.
- **A `db`-marked test that skips looks exactly like one that passed.**
  `REQUIRE_DB_TESTS=1` in CI converts the skip to a failure, which is the only
  reason the database tier cannot silently disappear. Locally the skip is
  correct — not every machine runs Docker — so the protection exists in exactly
  one place and is one environment variable deep.
- **Bubble field completeness cannot be fully automated** — the authority for
  "what fields exist" is the Bubble editor UI.

## Failure modes

`references/failure-modes.md` is this project's incident log. **Read it before any
non-trivial build, and add a row whenever something is found the hard way** —
including near-misses.

It is the most valuable thing in this overlay. Generic standards encode what
usually goes wrong; that file encodes what has actually gone wrong *here*.
