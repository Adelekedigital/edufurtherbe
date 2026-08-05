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
| 7 | Identity does not migrate. Accounts match on email; every user re-authenticates passwordlessly on first login — OAuth for Google or LinkedIn, and on the email path a **6-digit code by default with a sign-in link offered as a choice** (ADR 0009) | Bubble password hashes are not exportable, so re-authentication is a constraint rather than a preference. The mechanism is a code because a magic link fails predictably in this user base: Outlook Safe Links prefetches and consumes the token before the human clicks, and WhatsApp's in-app browser opens the link in a different session from the user's real one. Google was the dominant signup path, so for most of the 1,200 first login is one OAuth click and email is the minority case | Bubble ever exposes usable hashes |
| 8 | **Payments are out of scope for the initial build.** It delivers the core product backend and the Bubble migration; the legacy app has no payment integration to migrate | Nothing to carry across, and the pricing model is still being explored. Building a payment layer around an undecided model is how you get one you cannot change | The core backend and migration are done |
| 9 | A mentor sets their own **rate**; the platform offers a derived **pricing guide** as a suggestion | Mentors price their own time. The guide helps them choose without the platform setting the price | The platform ever takes pricing control |
| 10 | When payments arrive: money as integer minor units with an explicit currency; mentor payouts through an append-only **ledger**, paid by hand at first; the rate **snapshotted onto the purchase**, never read live from the profile | Floats do not represent money, and cross-currency is structural here. A ledger cannot be reconstructed after the fact. A live rate lookup means a mentor raising their price silently rewrites history | Never for the first two. The third only if rates become immutable |
| 11 | Payment provider: **undecided**, and not yet needed | Collections are African (Paystack/Flutterwave territory), payouts are global (Stripe/Wise territory). The two halves may not share a provider | — decide before payments work begins; it needs its own ADR |
| 12 | **Supabase** for Postgres, auth and storage, this phase. Used *as Postgres* — no PostgREST, no RLS as application logic (ADR 0005) | One vendor and one credential set during the cutover, and passwordless email login arrives without being built, which decision #7 needs for all 1,200 users. A policy in the database is invisible to `check_layers.py` and unreachable by a unit test, so authorization expressed there drifts from the Python that appears to implement it | Auth or storage needs to diverge from the database, or one shared failure domain across all three stops being acceptable |
| 13 | **FastAPI Cloud**, with Railway as the named fallback | Chosen deliberately. The escape stays real only by keeping a standalone Dockerfile exercised in CI and using no platform-native queue, cron or secrets store — an untested exit is an intention, not a plan | The platform cannot carry the freeze rehearsal, or a platform-native primitive becomes tempting |
| 14 | LLM access goes through a **provider-agnostic port**. No gateway | A port gives model swappability without a gateway's fee, extra hop, and flattening to the intersection of every provider. Adapters may use each provider's own caching, structured outputs and thinking configuration; the port must not degrade to the lowest common denominator | Never for the port. Which model is default is a config change, not a decision |
| 15 | Calendar is a **write target and an on-demand free/busy read** — never polled, never mirrored (ADR 0004) | Polling costs orders of magnitude more and is staler than reading at the moment of decision. Only mentors connect; mentees receive a calendar invitation and complete no OAuth flow | A proactive "your mentor's calendar now conflicts" feature is required, which on-demand reads cannot serve. Price the polling then |
| 16 | PWA push is **native Web Push** — VAPID keys, a service worker, no vendor | The browser does this. A notification platform earns its place at multi-channel fan-out with a preference centre, which is a later problem. iOS delivers push only to installed PWAs, so push is never the sole channel for anything that matters | A preference centre becomes a real requirement — then Novu, self-hosted |
| 17 | Institutions are the **hipolabs registry, populated on demand** (ADR 0008). Autocomplete is served live; a row is stored only once someone selects it, upserted on `domain`. Surrogate `uuid` primary key, `domain` as the natural key, **no `ror_id`** | Storing what is referenced gives ~200–400 rows rather than ~9,000. The dependency lands on **writes only** — reading existing education history never touches hipolabs, which is what the earlier ROR version of this row was trying to buy. Bubble already runs on hipolabs autocomplete, so this is the incumbent dependency rather than a new one taken on mid-migration. `education_entries.school_name_raw` is always kept, so an unmatched institution degrades display rather than losing data | Institution identity becomes a cross-system matching problem — mergers or renames breaking joins, or a partner exchanging institution records. Then ROR returns, and with it a backfill of every row collected in the meantime |
| 18 | WhatsApp is **platform→user transactional only**. Mentor↔mentee conversation stays in-platform (ADR 0006) | Handing both sides of a paid booking a direct off-platform channel is disintermediation by default rather than by decision | Never, while the platform takes a fee |
| 19 | ADRs use **Nygard** format, per ADR 0001 — *not* MADR | `/adr-new` ships with the tier-1 package and refers to the MADR template. Tier 1 is overwritten on update, so correcting the command would not survive; tier 2 does. Nygard is also the lower-ceremony format, and 0002 and 0003 already carry considered-options reasoning inside it without strain | ADR 0001 is superseded — which is itself an ADR |
| 20 | `docs/edufurther-migration/` is the **canonical target data model** — received, committed, and never edited here (ADR 0007) | It is the only artefact describing the target schema rather than the route to it, and part of its value is being a dated record from outside this repo. A transcribed copy would be a second copy that drifts from the DDL you actually run | A decision supersedes part of it — which is a new ADR, never an edit to the package |
| 21 | **Nothing ships earlier than the phase that creates a dependency on it.** In practice enums and lookups arrive with the phase that first uses them — with one standing exception: a foundation phase may carry a lookup whose rows the *next* phase's foreign keys require. `countries` and `languages` ship in M0 for exactly that reason; `institutions`, `degree_levels`, `service_offerings` and `scholarship_programs` wait for M2, and `legal_documents` for M1 | `00_foundation.sql` declares 40 enums and 7 lookup tables; exactly 2 of those enums are used by its own tables. Several encode business decisions this project has explicitly *not* made — `credit_source` contains `purchase` while payments are out of scope by decision #8 — and a shipped enum is a schema asserting a choice nobody took. ADR 0011 point 6 states the same rule in its shorter form, written before M0 was built; that record is accepted and immutable, so this row is where the exception lives | A phase genuinely needs a type before the table that constrains it |
| 22 | **ISO lookup tables are keyed on their natural key**, not a surrogate `id` — `countries.code`, `languages.code_639_3`. This overrides `persistence-patterns`, which requires a surrogate on every table | The ISO code is exactly what every foreign key stores. A surrogate would mean each FK holds a UUID that must be joined back to recover the code the caller already had. The generic rule buys uniformity; it stops paying when the key is externally standardised, stable, and already the thing being referenced | A lookup's key stops being externally governed, or starts changing |
| 23 | **The `updated_at` trigger is attached per table, in the migration that creates it** | The handoff used a blanket function that scanned and re-triggered every table in the schema. That is non-deterministic across environments, invisible in a diff, and on Supabase `public` is not exclusively ours. Explicit costs one line per table and can be forgotten — which is why a test asserts every table carrying `updated_at` actually has the trigger | Never. If the per-table line is tiresome, the answer is the test, not a scanner |
| 24 | **Reference tables carry no `legacy_bubble_id`** | The guardrail applies to *migrated* tables. `countries` and `languages` are seeded from a published standard, not carried across from Bubble, and a column null on all 7,327 rows is not a migration anchor | A reference table ever does originate in Bubble |
| 25 | **Alembic is the migration chain; the package DDL is a specification, never executed** (ADR 0011). SQLAlchemy 2.0 + asyncpg; models are the source of truth for tables and columns, everything autogenerate cannot see is hand-written | Ten `.sql` files that run against an empty database describe a destination; a chain has to move a *populated* database. The moment production holds a row those files can never run again, so adopting them would mean a chain with one link. Models also make `alembic check` and the every-model timestamp test possible, and both are guardrails this project already claims | Never for the chain. Whether a given migration is autogenerated or hand-written is a per-migration judgement |
| 26 | **M1 creates exactly eight tables**: `users`, `user_profiles`, `auth_identities`, `user_onboarding`, `user_languages`, `admin_users`, `legal_documents`, `user_legal_consents`. Excluded and each recorded in the migration: `auth_codes` + `password_hash` (ADR 0009), and — under decision #21 — the three `phone_*` columns, `calendar_connections` (M3), `account_deletion_requests`, and the `about_me` FTS index (M2) | Four package documents name four different M1 table sets, differing over `calendar_connections`, `credit_lots`, `user_legal_consents` and `account_deletion_requests`. ADR 0011 makes the DDL authoritative over the prose, so `01_identity.sql` wins — but it is silent on calendar, whose DDL sits in the availability file and whose legacy `composioAuthId` values are known-dead. Somebody had to name the set; this is it | A later phase needs one of the deferred objects, which is when it ships |
| 27 | **`legacy_bubble_id` belongs on tables derived from their own Bubble Thing**, not on every migrated table. In M1 that is `users` and `user_profiles` only | The guardrail below says "every migrated table", and it over-states. `user_onboarding`, `user_legal_consents`, `admin_users`, `auth_identities` and `user_languages` are all derived from *columns of the `User` row*; they have no Bubble id of their own to anchor on, and their idempotency key is `user_id` — already unique, already the primary key on three of them. A nullable column that is null on every row is not a migration anchor | A child table turns out to have its own Bubble Thing after all |
| 28 | **`users.slug` is carried**, nullable, with a partial unique index and a `^[a-z0-9-]+$` CHECK | It is the legacy public profile handle, populated on 39 of 43 dev users, and it appears in **no** package document and **not** in `docs/bubble-data-model.md` — found only by reading the extract. It is exactly the globally-unique human-readable key D32 says to avoid while multi-tenancy is deferred, so carrying it is accepting that cost knowingly: the alternative is breaking every live profile link. The CHECK is deliberately looser than the shape all 39 dev slugs take, because 43 rows are not 1,200 | Multi-tenancy arrives — then slugs need namespacing, and this row is the record of what that will cost |
| 29 | **Bubble's `Creation Date` lands in `created_at` and its `Modified Date` in `updated_at`**, loaded with `trg_set_updated_at` **disabled**. No `legacy_created_at` / `legacy_modified_at`; `legacy_bubble_id` is the only legacy column | One anchor column instead of a parallel timestamp space, and `created_at` then means "when this account began" on every row rather than "when the ETL ran" on 1,200 of them. The hazard the withdrawn columns addressed is real and now sits in the loader: this trigger is unconditional, so a re-running idempotent importer would otherwise stamp every migrated row with its own clock — silently, with row counts and null-rate reconciliation both still passing. M1c reconciliation asserts `updated_at` matches staging rather than trusting that somebody remembered | Never for the shape. If the loader proves unable to hold the trigger off reliably, the answer is a reconciliation failure, not a new column |
| 30 | **Closed vocabularies live in `domain/enums.py`**, as `StrEnum`, and `infra` imports them to build the PostgreSQL type. `infra/db/types.py::pg_enum` is the only way a model declares one | These are facts about the product — what roles exist, which providers can link — not facts about PostgreSQL, and a domain service that needs `PrimaryRole` cannot import `infra`. Defining them beside the models would guarantee a move the first time a business rule mentions one. The helper exists because SQLAlchemy's `Enum` sends member *names* to the database by default, which silently creates types whose labels disagree with the package and with every hand-written query; centralising it means that cannot be applied to four enums and forgotten on the fifth | Never. Where a type name must differ from the class name, `PG_ENUM_TYPES` carries it |
| 31 | **Those vocabularies are enforced as native PostgreSQL enum types, not as application-level validation.** The handoff's rule decides which vocabularies qualify: if adding a value requires a code change anyway → enum; if it does not → lookup table; if it is a standard → use the standard | Enforcing only in Pydantic would guard nothing during the phase we are in: **the ETL writes raw SQL**, so a transform bug could land `'Mentor'`, `'mentor '` or `'wizard'` in `primary_role` silently, and `psql` fixes bypass it too. Measured rather than assumed on PostgreSQL 17: `ALTER TYPE ... ADD VALUE` and `RENAME VALUE` both work **inside a transaction** on PG 12+, so the usual objection does not apply here. `text` + `CHECK` was the considered middle option — DB-level and trivially alterable — and loses because a CHECK cannot span tables, so M2–M6 would repeat and drift it per column | A vocabulary needs a value **removed**. `ALTER TYPE ... DROP VALUE` is *not implemented* in any PostgreSQL version — removal means a new type, re-pointing every column, and dropping the old one. That is the only expensive operation, and it is the trigger |
| 32 | **A 1:1 extension is keyed on its parent's id; a junction table is keyed on its natural pair.** `user_profiles.user_id`, `user_onboarding.user_id`, `user_languages (user_id, language_code)`. This overrides `persistence-patterns`' surrogate-id rule, as row #22 does for ISO lookups — #22 authorised it only for those, so this row covers the other two shapes | A surrogate on a strictly-1:1 extension gives one person two identifiers, and then every foreign key, function signature and API boundary has to decide which one it takes. The legacy system had exactly that defect — `mentor_profile_id` versus `user_id` — and the target model makes "`user_id` is the primary key of every 1:1 extension" one of its five principles. Four shapes now coexist deliberately: four tables generate their own `uuid_generate_v7()` id; `users` has an id it must be **given** (#7, ADR 0009 §9); three key on parent or pair; two key on an ISO code. Two tests assert that split, so adding a default anywhere goes red | A 1:1 extension stops being 1:1 — then it needs its own identity and this row no longer describes it |
| 33 | **A model module is named for the subject it owns, and models declare no `relationship()`.** `user.py`, `admin.py`, `legal.py`, `reference.py`; a new table joins the module whose subject it belongs to, or starts one | Without a rule, M2's `mentor_profiles` could defensibly go in `user.py`, a new `profiles.py`, or beside `user_profiles` — and five phases would answer differently, for 58 more tables. The absence of `relationship()` is currently doing real work and is invisible: it is why cross-module model imports stay acyclic, and under async a lazy-loaded relationship added later raises `MissingGreenlet` only after it ships. Loading is explicit at the query, per `persistence-patterns` | A genuine need for ORM-level traversal appears — then it is `selectinload`-style eager loading, declared deliberately, not a lazy default |

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
| **`institutions.domain`** | The hipolabs domain for an institution — what "which university" resolves to, and the natural key rows are upserted and deduplicated on. Null only for `source='manual'`, which is why it is not the primary key (ADR 0008) | Not the primary key — that is the surrogate `uuid`, and it is what another row points at. Not a university name either: a name is a display string. **There is no `ror_id`**; ADR 0008 supersedes the settled decision that defined one |
| **`whatsapp_conversation_id`** | Zernio's WhatsApp thread identifier, stable per participant and sending account, stored on the user record. Renamed by ADR 0007 — a vendor identifier carries the vendor's name | Not a message id. Unrelated to our own `conversation_id`, which is the primary key of our booking-scoped message threads (ADR 0006) and lives in our database |
| **`slug`** | The public profile handle on `users` — `sakiratu-adeleke`. Lowercase, digits and hyphens, unique among live users, and the thing a profile URL is built from. Carried over from Bubble, where 39 of 43 dev users have one | **Never an identifier to pass between layers, and never an authorization key.** It is user-visible, it is mutable, and it is globally unique in a system that has deliberately deferred multi-tenancy (#28). The primary key is `users.id`, which is the Supabase auth user id |

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
- [ ] **A new dependency's import name is added to
      `[tool.check-layers.forbidden-external]` in the same change.** The list is a
      denylist — default-allow — so an unlisted vendor is unguarded, and a vendor
      that arrives as a *transitive* of an extra is unguarded and unannounced. The
      guard also skips `main.py` and `api/deps.py`, which is where wiring lives, so
      it prevents an accidental import rather than a deliberate one
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
- [ ] Every table derived from its own Bubble Thing has `legacy_bubble_id`, and
      importers are idempotent on it. **Narrowed by decision #27** — a table
      derived from *columns of* a parent Thing anchors on the parent's foreign
      key instead, because it has no Bubble id of its own and a column null on
      every row anchors nothing
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
