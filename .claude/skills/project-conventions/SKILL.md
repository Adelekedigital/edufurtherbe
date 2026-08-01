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
| 6 | Bubble export is snapshot-first, and the transport sits behind a port (ADR 0002) | The freeze is short and Bubble goes away after; a transform bug must be re-runnable against the original bytes | Never |
| 7 | Identity does not migrate. Accounts match on email; every user does a magic-link or reset on first login | Bubble password hashes are not exportable. This is a constraint, not a preference | Bubble ever exposes usable hashes |
| 8 | **Payments are out of scope for the initial build.** It delivers the core product backend and the Bubble migration; the legacy app has no payment integration to migrate | Nothing to carry across, and the pricing model is still being explored. Building a payment layer around an undecided model is how you get one you cannot change | The core backend and migration are done |
| 9 | A mentor sets their own **rate**; the platform offers a derived **pricing guide** as a suggestion | Mentors price their own time. The guide helps them choose without the platform setting the price | The platform ever takes pricing control |
| 10 | When payments arrive: money as integer minor units with an explicit currency; mentor payouts through an append-only **ledger**, paid by hand at first; the rate **snapshotted onto the purchase**, never read live from the profile | Floats do not represent money, and cross-currency is structural here. A ledger cannot be reconstructed after the fact. A live rate lookup means a mentor raising their price silently rewrites history | Never for the first two. The third only if rates become immutable |
| 11 | Payment provider: **undecided**, and not yet needed | Collections are African (Paystack/Flutterwave territory), payouts are global (Stripe/Wise territory). The two halves may not share a provider | — decide before payments work begins; it needs its own ADR |

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
| **Booking** | A mentee claiming a seat in a specific slot | Not a session — a booking can be cancelled before it ever occurs |
| **Session** | Mentorship that actually took place | Not a booking |
| **Rate** | What a mentor charges, set by the mentor | Not the pricing guide |
| **Pricing guide** | A platform-computed *suggestion* derived from the mentor's experience and profile | **Not a price.** Advisory only, never persisted as the agreed amount |
| **`legacy_id`** | The Bubble `_id` a row came from | Not our primary key, and never exposed in an API |

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
  primary key, the `legacy_id` from Bubble, and any provider-side id (payment
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
      not people
- [ ] Every migrated table has `legacy_id`; importers are idempotent on it
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
uv run uvicorn app.main:app --reload --app-dir src      # run
uv run pytest                                           # tests
make check                                              # the full local gate
uv run python scripts/check.py                          # the same gate, without make
```

## Where the truth lives

| Doc | Covers | Canonical copy |
|---|---|---|
| `CLAUDE.md` | Router; the few always-true facts | repo root |
| This file | Settled decisions, vocabulary, guardrails | `.claude/skills/project-conventions/` |
| `docs/adr/` | Decisions and their rationale | repo root |
| `references/failure-modes.md` | What has actually gone wrong here | alongside this file |
| `docs/bubble-data-model.md` | Legacy Bubble shape, fields, and row counts | repo root |
| `README.md` | Human-facing setup and layout | repo root |

**Canonical copy rule.** Where a doc is duplicated, **name the one that wins** and
edit only that. Duplicated facts drift, and the copy you forget becomes wrong.

## Enforcement blind spots

State a gate's blind spots next to its coverage, every time.

- **Branch protection does not exist yet.** CI runs on pull requests and
  `no-commit-to-branch` blocks local commits to `main`, but nothing is enforced
  server-side. Until a GitHub ruleset requires a PR and passing checks, this is
  convention that catches accidents, not a control that stops intent. There is no
  CODEOWNERS file despite `.pre-commit-config.yaml` citing one.
- **There is no dataflow analysis.** CodeQL is absent from the Security workflow
  because uploading results needs GitHub Advanced Security, which a private
  personal repo does not have. bandit, ruff's `S` ruleset and pip-audit cover
  pattern-matched issues and known CVEs; **nothing tracks tainted input across
  function boundaries.** Restore by enabling GHAS or making the repo public.
- **`gitleaks` finds credentials, not PII.** It will not stop a member export.
- **The layer check reads imports, not behaviour.** It cannot see a domain rule
  implemented in `api/`.
- **Bubble field completeness cannot be fully automated** — the authority for
  "what fields exist" is the Bubble editor UI.

## Failure modes

`references/failure-modes.md` is this project's incident log. **Read it before any
non-trivial build, and add a row whenever something is found the hard way** —
including near-misses.

It is the most valuable thing in this overlay. Generic standards encode what
usually goes wrong; that file encodes what has actually gone wrong *here*.
