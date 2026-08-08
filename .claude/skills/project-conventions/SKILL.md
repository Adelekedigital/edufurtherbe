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
| 22 | ~~**ISO lookup tables are keyed on their natural key**~~ — **REVERSED by ADR 0015.** `countries` and `languages` now carry a generated surrogate `id` like every other table; `code` and `code_639_3` remain `NOT NULL` and `UNIQUE`, and foreign keys store the id | The original argument still reads well and is preserved in ADR 0015: the ISO code is externally standardised and already the value a foreign key would store, so a surrogate means holding a UUID that must be joined back. It lost to a different consideration — **a rule with exceptions costs every reader the memory of the exceptions**, and two tables out of sixty-six behaving differently is what gets rediscovered and re-decided in M3. At this volume the join is free; the ambiguity is not | Never by argument. Only by an ADR superseding 0015 |
| 23 | **The `updated_at` trigger is attached per table, in the migration that creates it** | The handoff used a blanket function that scanned and re-triggered every table in the schema. That is non-deterministic across environments, invisible in a diff, and on Supabase `public` is not exclusively ours. Explicit costs one line per table and can be forgotten — which is why a test asserts every table carrying `updated_at` actually has the trigger | Never. If the per-table line is tiresome, the answer is the test, not a scanner |
| 24 | **Reference tables carry no `legacy_bubble_id`** | The guardrail applies to *migrated* tables. `countries` and `languages` are seeded from a published standard, not carried across from Bubble, and a column null on all 7,327 rows is not a migration anchor | A reference table ever does originate in Bubble |
| 25 | **Alembic is the migration chain; the package DDL is a specification, never executed** (ADR 0011). SQLAlchemy 2.0 + asyncpg; models are the source of truth for tables and columns, everything autogenerate cannot see is hand-written | Ten `.sql` files that run against an empty database describe a destination; a chain has to move a *populated* database. The moment production holds a row those files can never run again, so adopting them would mean a chain with one link. Models also make `alembic check` and the every-model timestamp test possible, and both are guardrails this project already claims | Never for the chain. Whether a given migration is autogenerated or hand-written is a per-migration judgement |
| 26 | **M1 creates exactly eight tables**: `users`, `user_profiles`, `auth_identities`, `user_onboarding`, `user_languages`, `admin_users`, `legal_documents`, `user_legal_consents`. Excluded and each recorded in the migration: `auth_codes` + `password_hash` (ADR 0009), and — under decision #21 — the three `phone_*` columns, `calendar_connections` (M3), `account_deletion_requests`, and the `about_me` FTS index (M2) | Four package documents name four different M1 table sets, differing over `calendar_connections`, `credit_lots`, `user_legal_consents` and `account_deletion_requests`. ADR 0011 makes the DDL authoritative over the prose, so `01_identity.sql` wins — but it is silent on calendar, whose DDL sits in the availability file and whose legacy `composioAuthId` values are known-dead. Somebody had to name the set; this is it | A later phase needs one of the deferred objects, which is when it ships |
| 27 | **`legacy_bubble_id` belongs on tables derived from their own Bubble Thing**, not on every migrated table. In M1 that is `users` and `user_profiles` only | The guardrail below says "every migrated table", and it over-states. `user_onboarding`, `user_legal_consents`, `admin_users`, `auth_identities` and `user_languages` are all derived from *columns of the `User` row*; they have no Bubble id of their own to anchor on, and their idempotency key is `user_id` — already unique, already the primary key on three of them. A nullable column that is null on every row is not a migration anchor | A child table turns out to have its own Bubble Thing after all |
| 28 | **`users.slug` is carried**, nullable, with a partial unique index and a `^[a-z0-9-]+$` CHECK | It is the legacy public profile handle, populated on 39 of 43 dev users, and it appears in **no** package document and **not** in `docs/bubble-data-model.md` — found only by reading the extract. It is exactly the globally-unique human-readable key D32 says to avoid while multi-tenancy is deferred, so carrying it is accepting that cost knowingly: the alternative is breaking every live profile link. The CHECK is deliberately looser than the shape all 39 dev slugs take, because 43 rows are not 1,200 | Multi-tenancy arrives — then slugs need namespacing, and this row is the record of what that will cost |
| 29 | **Bubble's `Creation Date` lands in `created_at` and its `Modified Date` in `updated_at`**, loaded with `trg_set_updated_at` **disabled**. No `legacy_created_at` / `legacy_modified_at`; `legacy_bubble_id` is the only legacy column | One anchor column instead of a parallel timestamp space, and `created_at` then means "when this account began" on every row rather than "when the ETL ran" on 1,200 of them. The hazard the withdrawn columns addressed is real and now sits in the loader: this trigger is unconditional, so a re-running idempotent importer would otherwise stamp every migrated row with its own clock — silently, with row counts and null-rate reconciliation both still passing. M1c reconciliation asserts `updated_at` matches staging rather than trusting that somebody remembered | Never for the shape. If the loader proves unable to hold the trigger off reliably, the answer is a reconciliation failure, not a new column |
| 30 | **Closed vocabularies live in `domain/enums.py`**, as `StrEnum`, and `infra` imports them to build the PostgreSQL type. `infra/db/types.py::pg_enum` is the only way a model declares one | These are facts about the product — what roles exist, which providers can link — not facts about PostgreSQL, and a domain service that needs `PrimaryRole` cannot import `infra`. Defining them beside the models would guarantee a move the first time a business rule mentions one. The helper exists because SQLAlchemy's `Enum` sends member *names* to the database by default, which silently creates types whose labels disagree with the package and with every hand-written query; centralising it means that cannot be applied to four enums and forgotten on the fifth | Never. Where a type name must differ from the class name, `PG_ENUM_TYPES` carries it |
| 31 | **Those vocabularies are enforced as native PostgreSQL enum types, not as application-level validation.** The handoff's rule decides which vocabularies qualify: if adding a value requires a code change anyway → enum; if it does not → lookup table; if it is a standard → use the standard | Enforcing only in Pydantic would guard nothing during the phase we are in: **the ETL writes raw SQL**, so a transform bug could land `'Mentor'`, `'mentor '` or `'wizard'` in `primary_role` silently, and `psql` fixes bypass it too. Measured rather than assumed on PostgreSQL 17: `ALTER TYPE ... ADD VALUE` and `RENAME VALUE` both work **inside a transaction** on PG 12+, so the usual objection does not apply here. `text` + `CHECK` was the considered middle option — DB-level and trivially alterable — and loses because a CHECK cannot span tables, so M2–M6 would repeat and drift it per column | A vocabulary needs a value **removed**. `ALTER TYPE ... DROP VALUE` is *not implemented* in any PostgreSQL version — removal means a new type, re-pointing every column, and dropping the old one. That is the only expensive operation, and it is the trigger |
| 32 | **Every table has `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`. No exceptions** (ADR 0015). A 1:1 extension carries `UNIQUE (user_id)`; a junction carries a unique index on its natural pair; a demoted natural key keeps its own `UNIQUE`. **A future exception is an ADR superseding 0015, never a row here** | This rule already existed in tier-1 `persistence-patterns` and was overridden twice anyway — once for ISO lookups (#22) and once for 1:1 extensions and junctions — with every gate green through both. The overrides were argued, recorded, reviewed and merged; that is what makes the lesson general. **A convention enforced only by prose is re-decided by whoever reads it next**, and each re-decision is locally defensible. It is now enforced by a test that walks the live schema and rejects a composite key, a key not named `id`, and an id the caller must supply — each proved by reintroduction | Never |
| 33 | **A model module is named for the subject it owns, and models declare no `relationship()`.** `user.py`, `admin.py`, `legal.py`, `reference.py`; a new table joins the module whose subject it belongs to, or starts one | Without a rule, M2's `mentor_profiles` could defensibly go in `user.py`, a new `profiles.py`, or beside `user_profiles` — and five phases would answer differently, for 58 more tables. The absence of `relationship()` is currently doing real work and is invisible: it is why cross-module model imports stay acyclic, and under async a lazy-loaded relationship added later raises `MissingGreenlet` only after it ships. Loading is explicit at the query, per `persistence-patterns` | A genuine need for ORM-level traversal appears — then it is `selectinload`-style eager loading, declared deliberately, not a lazy default |
| 34 | **A module ships with its routers.** From M2 onward, a phase delivers schema, transform *and* the endpoints that read it — not six phases of migration followed by an API. The contract shapes are settled: RFC 9457 Problem Details, cursor pagination, normalisation at the boundary (ADR 0016) | Migrating everything first means the schema is never exercised by real usage until changing it is expensive. It also defers the two contract decisions past the point where they are free — tier 2 had already recorded that the error envelope and pagination must be settled before the *second* endpoint, and six phases of schema would have buried that | A phase genuinely has no read surface — then say so in the phase, rather than silently shipping none |
| 35 | **Every route carries a `summary`, a `description` and its documented failure responses; every tag carries a description of the group** | `/docs` is the contract the Next.js client is built against. A reader arriving cold should learn what a group is for and how a call fails without opening a route module — and the descriptions are the only part of the contract that survives someone reading the schema rather than the code. Nothing enforces it: a test walking the OpenAPI schema for missing summaries would, and does not exist yet | Never |
| 36 | **Normalisation happens at the boundary; the database constrains rather than transforms.** Pydantic normalises on the way in from the API, `domain/transform.py` on the way in from Bubble, and the column carries a `CHECK` asserting what both already guarantee | `users.email` was `citext` so comparison would be case-insensitive everywhere. Both writers already lowercase, so the type insured against a case that needs a *third* writer — and that writer is better served by a constraint that **refuses** than a type that silently accepts a second spelling nobody can find again. It also generated a Supabase advisory whose stated risk ADR 0005 had already removed. Declarative, on a shared schema mixin, so a new route cannot omit it by not thinking about it | A normalisation genuinely cannot be expressed at the boundary — then it is a domain rule, not a column type |
| 37 | **One canonical record, whichever source produced it.** `JsonExportSource` and `BubbleApiSource` both satisfy `domain.bubble.BubbleSource`; the transform never learns which it got. They differ on exactly six things and each adapter absorbs its own: the id field, whether email is flat or nested under `authentication`, array versus comma-joined list, `""` versus an absent key, timestamp format, and `Creation Date` versus `Created Date` | Every difference would otherwise be a branch in the mapping code, and a branch on provenance is a branch somebody forgets on one path. The sixth was found only by dry-running the loader against the real export, which refused all 43 records — reading two formats side by side shows what each *contains*, not what a consumer will look for and fail to find | A third source appears; it gets an adapter, not a branch |
| 38 | **Bubble timestamps go through one function, and it refuses to guess.** The API emits ISO-8601 `Z`; the export emits `Sep 3, 2025 5:31 am` with **no offset at all**, so `parse_timestamp` requires an explicit zone on that path and raises without one. The export renders in `America/New_York` — **measured, not assumed**: user `1701974206179x877854702892984200` reads `Dec 7, 2023 1:36 pm` in the export and `2023-12-07T18:36:46.179Z` from the API | A missing offset is absent data, not a parsing problem, and no cleverness recovers it. Defaulting to UTC is the one genuinely dangerous option: it would shift every migrated `created_at` by five hours — plausible-looking, wrong, and invisible to every row-count and null-rate check the runbook specifies | The Bubble app's timezone setting changes, which nothing here would notice |
| 39 | **`tzdata` is a runtime dependency, not an optional extra** | `zoneinfo` reads the operating system's database and **Windows has none** — `available_timezones()` returns zero, so every `ZoneInfo("Africa/Lagos")` raises on a developer machine while CI on ubuntu passes, and a slim container image usually fails the same way. `users.timezone` stores IANA names, a guardrail requires the mentor's zone as its own column, and M3 availability is entirely timezone conversion. Pinning the data makes the three environments agree instead of differing by platform | Never |
| 40 | **Auth accounts are provisioned eagerly, before cutover — not lazily at first login** (ADR 0018, closing the question ADR 0014 left open). Every mode of `scripts/provision_auth.py` is idempotent, and re-running is the recovery plan rather than a rollback | Eager is the only version you can prove works *before* the freeze: it runs end to end against a rehearsal database and reports a count. Lazy surfaces a linking defect one user at a time, after go-live, which is the worst possible moment to discover it. There is no undo for creating 1,200 auth accounts, so idempotence is not a convenience — it is the entire recovery story. A row already linked is skipped **without consulting Supabase at all**, so a re-run of 1,200 provisioned users costs zero API calls | Provisioning ever needs to happen for a population too large to run in one freeze window |
| 41 | **`--grant-admin` has no authorization check of its own, and that is the bootstrap rule.** The authority is **possession of the database credentials** — and only those. The mode never contacts Supabase and needs no service-role key. Every CLI grant lands in `admin_users` with `granted_by` null | There is no first admin to approve the first admin, so the chain has to start out of band. **The service-role key is deliberately not a second factor here** (ADR 0018, "The bootstrap grant"), and an earlier draft of this row claimed it was: anyone holding the DSN can `INSERT INTO admin_users` directly, so requiring the key in the CLI would document a control the system does not have — the exact failure this file logs for the `calAccessToken` claim. Null `granted_by` is the honest record of a grant made out of band — the same value every migrated grant carries, because the legacy option set recorded that someone was an admin and never who made them one. A synthetic "system" actor would look like knowledge we do not have, in the one table whose entire purpose is being auditable | An in-product admin-management surface exists — then grants made through it carry a real `granted_by`, and this stays the bootstrap path only |
| 42 | **When an adjacent vendor endpoint has an irreversible real-world side effect, name it in the adapter and let a test assert it is never called.** `infra/auth/admin.py` defines `INVITE_ENDPOINTS` and never uses it | Supabase has three ways to create a user and two of them send email. Reaching the wrong one does not raise, roll back or fail a health check — it delivers 1,200 emails to people who have not been told the platform moved, and there is no recall. A constant that exists only to be asserted *absent* converts "we meant to use the silent one" into a test that goes red on the mutation, which is the strongest guarantee available short of the vendor not offering the endpoint | Never — and the same shape applies to any future send, charge or delete |
| 43 | **One rule, one representation — and the copies that hurt are the ones no tool can see.** A predicate, mapping, constant or test double in a second place is a defect. Where a copy genuinely cannot be removed, the copies are pinned by a test that fails when they diverge, as `EXPORT_TIMEZONE` already is | Three of this project's settled decisions are *instances* of this — `pg_enum` exists "so it cannot be applied to four enums and forgotten on the fifth", `scripts/check.py` is "the single definition of the gate", one canonical Bubble record so a branch on provenance "is a branch somebody forgets on one path" — and **the rule above them was never written**, so each new case was decided from scratch and each decision was locally defensible. Then it cost twice in one unit: `deleted_at IS NULL` hand-typed into five statements with the `UPDATE` missed, and three private copies of one test double all carrying the same omission. Neither is visible to ruff, mypy, `check_layers` or coverage — a predicate inside SQL is not a symbol, and a fake is just more test code | Never. The *mechanism* is negotiable — extract, or pin with a test — the single representation is not |
| 44 | **`scripts/*.py` is a third composition root, and the gates do not cover it.** A script may construct concrete `infra` classes and wire them together. It may contain **no SQL and no business rule** | CLAUDE.md said `api/deps.py` and `main.py` were the only wiring points, and that was already false — `load_identity.py` builds an engine and a source. The real hazard is narrower and sharper: `scripts/check.py` runs `mypy src` and `bandit -r src`, and coverage measures `src/app`, so anything written under `scripts/` is checked by **ruff alone**. Provisioning put ~250 lines of async SQLAlchemy there, exercised by 650 lines of integration tests that counted for nothing against the coverage floor — the test file could have been deleted with the gate still green | The gate is extended to cover `scripts/`, at which point the SQL clause can relax and the composition-root clause still holds |
| 45 | **A result aggregate and its operator-facing rendering live in `domain/`**, beside the decision they summarise — `provisioning.Outcome`, not the store. `infra/etl/reconcile.Reconciliation` is the outlier and stays where it is | Two identical concerns in two layers is what a later author reads as "either is fine", and the one they pick is whichever file they saw last. `Outcome` is pure — counts and strings, no I/O — so the layer rule permits it in either place, which is precisely why prose has to choose. `domain/` wins because the counts *are* the decision's result, and a store that formats text for a terminal has stopped being a store | `Reconciliation` is next touched — it moves then, rather than being churned now for symmetry |
| 46 | **CORS is explicit origins, credentialed, and absent when unconfigured.** `CORS_ORIGINS` accepts a JSON array *or* a comma-separated list; empty or unset installs no middleware at all | The setting broke a deploy while wired to nothing: pydantic-settings JSON-decodes a complex type inside the settings source, before validators, so a bare origin failed as an opaque `SettingsError` at boot. Explicit lists are also forced rather than chosen — Starlette refuses `*` for origins, methods or headers once `allow_credentials=True`, and the frontend sends `Authorization` | Origins ever need to be wildcarded, which would mean dropping credentials |
| 47 | **Only environment keys generic enough to collide carry the `EDUFURTHER_` prefix** — `ENVIRONMENT` and `DEBUG`, and nothing else. `SUPABASE_URL`, `DATABASE_URL`, `CORS_ORIGINS` and the rest stand alone | A prefix on `SUPABASE_URL` buys nothing: it already names its vendor. On `DEBUG` it buys a great deal, because a host, a base image or another tool may define that word first. `DATABASE_URL` unprefixed is a small bonus — most managed platforms inject exactly that name. **The cost is real and is written into `config.py`:** the old blanket prefix let one validator prove any misspelling was a typo, and that now holds only for the two prefixed keys. A misspelled `SUPABSE_URL` is indistinguishable from an unrelated variable and leaves the default silently in place | A third key turns out to be generic enough to collide — it joins `PREFIXED_FIELDS`, and the guard follows automatically |
| 48 | **Verification depth scales with what a mistake costs** — the three tiers in CLAUDE.md. A checklist and approval come first at *every* tier | Everything was being verified as though it were the provisioning CLI: watch-fail, a twelve-way mutation batch, a stress run against real data, two review agents. That is right for 1,200 irreversible auth accounts and absurd for renaming an environment variable, and the uniform cost is a real part of why the early migration felt slow. The tier is stated in the checklist so it is agreed rather than assumed | Never — but a *specific* tier assignment is always arguable, and arguing it is cheap |
| 49 | **Two questions before writing code, beyond the generic rules in CLAUDE.md: where does the SQL live, and does this rule already exist somewhere?** SQL belongs in `infra/`, never `scripts/`. A rule that exists is imported, not retyped | Both were known and both were violated anyway, and each cost a whole extra pull request: SQL written into `scripts/` where no gate can see it, and `deleted_at IS NULL` retyped into a fifth statement that then missed it. Neither was a hard problem — they were questions nobody asked at the point where asking is free | Never |
| 50 | **A clarifying question offers 2–4 concrete options, a recommendation first, and one line on what each costs.** Plain words, no survey | CLAUDE.md rule 5 already says a genuine choice comes back for approval; it says nothing about *how*, and an unstructured question is one the reader has to do the analysis for. Naming the options and their consequences is what makes the answer a decision rather than a guess — and what lets it be given in one line | Never |
| 51 | **One fact, one home.** If it is in an ADR, the docstring is a pointer. If it is in a row here, the module does not restate the argument | The eager-provisioning rationale existed in an ADR, a settled-decision row and two docstrings at once — four copies of one argument, which is non-negotiable #8 applied to prose. Copies drift, and the reader cannot tell which is authoritative | Never |
| 52 | **Migrations are applied by the `Migrate` workflow, dispatched by hand — never on application startup, and never automatically on merge** (ADR 0017) | The serving platform has no release hook, but the deeper reason is independence: the workflow connects to Supabase directly, so changing host changes nothing about how a migration is applied. Dispatch-only because a schema change landing without anybody choosing the moment is how a lock queue meets a traffic peak; `production` additionally refuses any ref but `main` | A platform gains a release hook *and* the cutover no longer needs a rehearsable mechanism |
| 53 | **`service_offerings` is a closed six-row vocabulary — the *parents* of the legacy option set — and users cannot extend it.** No `status`, no `merged_into_id`, no `usage_count`, no `created_by`, and **no `parent_id` until children actually exist** | Package D12 says Bubble held two *separate* option sets with no mapping. It held **one**, used by both sides — but both columns store the display name as text at the moment of selection, so the mentee column carries six parents while the mentor column carries five parents plus five children and renames. Collapsing to the six parents is what makes a mentee's need and a mentor's offer the same row; the mixed-depth list is precisely why matching never worked, since "Document Review" could never meet "document preparation". Closed because this is the **matching axis**: free text turns "SOP help", "Statement of Purpose" and "sop" into three rows that match nothing, and the join simply returns fewer results with nothing to say why. The long tail lives where it belongs — `institutions` and `scholarship_programs` are both open, with the merge machinery to match | Children ship — they get the merge machinery and users may name an unlisted test or scholarship, **never a new top-level category**. Splitting or renaming a parent is the expensive move and has no `merged_into_id` behind it, so all future specificity goes into children |
| 54 | **A model module is named for its subject and splits at ~500 lines or 7 models** — `education.py`, `mentoring.py` and `scholarships.py` join `user.py`, `admin.py`, `legal.py` and `reference.py`. `mentoring.py` holds mentor *and* mentee tables, because `service_offerings` joins them and belongs to neither | Decision #33 names the rule and gives no threshold, so "is this module too big" stayed a fresh judgement every phase, for 58 remaining tables. `user.py` is 389 lines for 5 models, which sets the scale. The split is **by subject, never by size**: if `mentoring.py` outgrows the bound, mentor and mentee separate and `service_offerings` moves to its own module rather than being left in whichever half happens to be smaller | Never for the rule. The numbers are a tripwire for a conversation, not a limit to enforce mechanically |
| 55 | **`institutions.status` and `scholarship_programs.status` default to `'approved'`, so every user-facing write path must set `'pending_review'` explicitly** until admin curation ships | The package's default is fail-open: a write path that forgets publishes a user-created row globally and nothing objects. It is kept anyway, because flipping it before any write path exists would be a departure argued against code nobody has written. The obligation therefore sits on the writer — recorded here and in the migration docstring — and `test_status_defaults_to_approved` turns the eventual flip into a deliberate edit rather than a silent behaviour change | The pull request that ships admin curation: the default flips to `'pending_review'` there, and this row is the record that it was a known gap rather than an oversight |
| 56 | **Counts and ratios are derived at query time, never stored.** No cached totals, no denormalised counters, no percentage columns | Demonstrated twice and written down neither time. The package drops `countCompletedSession`, `countReviewReceived` and `percentageOfCompletedSession` from `Mentor (front search)` as "DERIVED at query time", and `mentor_profiles` explains why in its own comment: storing a derivable value "recreates the exact drift that made the front-search table untrustworthy". Then `usage_count` shipped anyway on two lookups — declared, indexed, explained as the admin queue's ranking signal, and **maintained by nothing**: no trigger, no function, no application rule, so it was zero on every row forever. Worse than useless, because the number exists to inform an approve-or-merge judgement and a stale one misleads exactly that. At 200–400 rows a `COUNT(*)` over a join is free | A count is genuinely too expensive to derive — which needs a measurement, not an intuition. Then it is a materialised view before it is a column, because a view states its own staleness |
| 57 | **`requires_booking_confirmation` defaults to `false`**, where the package has `true`, and the same value applies to migrated and new mentors | Legacy stored a blank on 10 of 15 mentors, and blank meant "never turned it on" — so `false` is what the data says. Making migrated and new mentors start on opposite settings would be a difference nobody could explain later. The exposure is bounded and that is what makes it safe rather than merely convenient: a mentor is bookable only when **approved** *and* **listed**, both of which they opted into, and in M3 a mentor with no availability rules has no bookable slots at all. `test_booking_confirmation_defaults_to_false` makes reverting it a deliberate edit | Auto-confirm produces a real complaint from a real mentor. It is one column default and a migration away |
| 58 | **Legacy `meetingDuration` is an M4 input, not an M2 column.** It becomes the `duration_minutes` of the auto-created "General Mentorship" session type each migrated mentor receives | `02_profiles.sql` says mentor-level duration is "dropped" **fifteen lines below** the note that every existing mentor gets an auto-created session type in M4 — which is exactly where a mentor's chosen 15/30/45/60 belongs. The two lines were never connected, so following either one alone loses real data or invents a column the package refused. Duration is a property of the *offering*, not a global preference: a 15-minute question and a 60-minute document review have no meaningful shared default, which is why venue and confirmation cascade and this does not | Never for the shape. If M4 ships without reading it from the staged snapshot, the data is gone and this row is the evidence it was known |
| 59 | **`user_scholarship_experience` and the `scholarship_relationship` enum never ship.** `user_awards.scholarship_program_id` carries the credential instead | The package pairs `user_awards` with a second table keyed on `awarded \| applied \| advised`. The legacy field behind it — `Mentor Services.Scholarship Experience` — has **no option set and no values on any row**, so there is no vocabulary and nothing to migrate; and it overlapped `user_awards`, giving "I won Chevening" two legal homes with nothing choosing between them. Dropping it left `scholarship_programs` with no consumer anywhere in the sixty-six-table package, which is why the nullable link was added: it restores one, and takes the `school_name_raw` + `institution_id` shape where the raw text is always kept and display never depends on the link. What is genuinely deferred is narrower than it looks — *applied* is a mentee goal and *advised* is a mentor capability, and both belong to the service hierarchy rather than here | A mentor needs to say "I advise on Chevening". That is the child level of `scholarships-financial-aid`, not a resurrected table |

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

- **Configuration:** everything through `core/config.py`. Only `ENVIRONMENT` and
  `DEBUG` are `EDUFURTHER_`-prefixed; the rest name their own subject.
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
- [ ] **Credential fields are dropped at extraction, before anything is
      written** — `calAccessToken`, `calRefreshToken` and their expiry columns.
      **They are expired Cal.com tokens from the abandoned integration, not live
      Google credentials**: decoded, both access tokens expired over 400 days ago
      and both refresh tokens over 50. Three records asserted otherwise, inferred
      from the field names. Redaction stays because it costs a field list and
      makes the snapshots boring, not because there is an incident. `composioAuthId`
      is **not** redacted — it is a vendor reference, not a token, and it migrates
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
- [ ] **Every table added carries `id uuid PRIMARY KEY DEFAULT uuid_generate_v7()`**, and any invariant a natural or composite key would have carried is re-declared as `UNIQUE`. Losing that constraint is silent — duplicates become legal and nothing surfaces it until somebody reads the wrong row
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
- **`alembic check` compares foreign keys by their column signature, never by
  their name.** So a constraint whose name in the database differs from the name
  in the source file is invisible to it, and to everything else in the gate —
  nothing reads constraint names at all. That is how a 65-character key shipped
  under a truncated, hashed name that appeared nowhere in the repository, with
  six green CI checks and a thirteen-way mutation batch. `test_no_declared_
  identifier_exceeds_the_postgresql_limit` closes the length case at declaration
  time; **a name written directly into a migration and into no model is still
  unguarded**, because the test walks `Base.metadata` and a migration is a
  separate artefact.
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
- **Nothing detects duplication, and the copies that matter are invisible in
  principle.** No gate step measures it. Worse, the two shapes that have
  actually cost this project are ones no static tool could see: a predicate
  inside a `text()` string is not a symbol mypy or ruff can bind, and a test
  double is just more test code. `test_provisioning_store.py` closes exactly
  one instance — statements in the provisioning store that filter `users` —
  by walking the module rather than a declared list, so a new statement cannot
  quietly go unchecked. **Nothing generalises that to the next module.** It is
  review against decision 43, and that is the whole of the enforcement.
- **Nothing stops an ordinary `UPDATE` from destroying a migrated timestamp.**
  `users.updated_at` carries Bubble's Modified Date, and `trg_set_updated_at`
  fires on every update, so any statement that touches a migrated row rewrites
  it to the import clock — silently, with nothing to restore it from. The ETL
  and the provisioning CLI both hold the trigger off via
  `infra/etl/loader.timestamps_from_source`; **nothing makes the next writer do
  the same.** It is caught only by a per-caller test asserting a known past
  timestamp survives, and only where somebody thought to write one.
- **Bubble field completeness cannot be fully automated** — the authority for
  "what fields exist" is the Bubble editor UI.

## Failure modes

`references/failure-modes.md` is this project's incident log. **Read it before any
non-trivial build, and add a row whenever something is found the hard way** —
including near-misses.

It is the most valuable thing in this overlay. Generic standards encode what
usually goes wrong; that file encodes what has actually gone wrong *here*.
