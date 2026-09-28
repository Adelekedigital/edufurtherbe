# Handoff — scoping M6 (Communications)

**Status:** scoping only. Nothing decided, nothing built.
**Written:** 2026-08-27, from the dev Bubble Data API measured against the
codebase and `docs/edufurther-migration/`.
**Companions:** `handoff-review-build.md` (M5a, shipped),
`handoff-credit-build.md` (M5b, shipped).

This is deliberately **not** a build plan. M5a and M5b each got one only after
their blocking questions were answered; M6's have not been asked yet. What
follows is the measurement, the one decision that has to come first, and the
transition notes that would otherwise be rediscovered by running into them.

---

## The one-line state

**M6 is not a migration phase.** The package files it as Communications, ~750
rows, low risk, no downstream dependencies. That framing does not survive
contact with the codebase:

> **There is no notifications table.** ADR 0025 built *delivery* — a taxonomy,
> two senders, template ids — and no persisted record of anything sent.

So D22's design has never been built. The 681-row ETL the runbook describes has
no destination, and "migrate notifications" is downstream of a build decision
nobody has taken.

**The nav bell has no backend.** `CreditUI.png` shows a bell in the top-right on
every screen. Nothing behind it exists: no table, no read surface, no unread
count.

---

## What was measured

Pulled from the **dev** Data API on 2026-08-27. Production is held back until the
core backend build is done, which is settled policy — an unrehearsed loader is
the expected state of every loader until cutover, never a blocker.

### `🔔notification` — 390 rows in dev, 681 in production

| Field | Populated | What it means |
|---|---|---|
| `bubble_id`, `created_at`, `modified_at` | 390 | — |
| `👁️ Seen sender` | 390 | — |
| `Created By` | 366 | |
| `Notify Type` | **322** | **68 rows have no type at all** |
| `👁️ Seen receiver` | 310 | |
| `Receiver(list of users)` | 301 | the fan-out recipient field |
| `Receiver` | 259 | the single recipient field — overlaps the above |
| `Notification Title` | **170** | |
| `Notification Sender body` | **170** | |
| `Seen(list of users)` | **13** | the runbook's source for `read_at` |

Three findings that change the plan:

1. **220 of 390 notifications have no title and no body.** Fifty-six per cent of
   the table is a row recording that *something* happened, with nothing to
   render. Migrating them produces an inbox of blanks.
2. **`Session` and `Review` are populated on zero rows.** The runbook says "map
   `Session` and `Review` to the typed FKs"; in dev there is nothing to map, so
   D22's typed-FK half has no dev evidence behind it. Confirm against production
   before building columns for it.
3. **Read state is nearly absent** — 13 rows of 390 carry `Seen(list of users)`.
   `read_at` would be null on 97% of migrated rows.

Two recipient fields — `Receiver` and `Receiver(list of users)` — overlap and
neither is complete. That is the shape D22 was reacting to, and it is what
`notification_recipients` exists to normalise.

### `messagestarter` — 2 rows in dev, 13 in production

`sendBy` · `receiveBy` · `lastMessageContent` · `messageRequestAccept`

**`messageRequestAccept` is a request/accept gate the package never mentions.**
A conversation is *asked for* before it opens. D29 designs `conversations` +
`conversation_participants` + `messages` and says nothing about a pending state,
so the vocabulary is missing a value the legacy data already carries.

### `messagethread` — 4 rows in dev, 44 in production

`messageContent` · `messageStarter` (FK to the starter) · `sendBy` ·
`receivedBy` · `seenRead` · `sentAt` · `dateLastEdited`

`dateLastEdited` on 1 of 4 — messages are **editable** in the legacy app. Another
behaviour with no home in D29.

---

## The decision that has to come first

**Is in-app messaging being built at all?**

Everything else in M6 is downstream of it, and the arguments do not point one way:

| For building it | Against |
|---|---|
| D29 chose in-house, deliberately, with reasons | D29 predates Loops shipping and predates every notification adapter in M4 |
| The nav carries a **Messages** item | **No messaging screens exist** in `FE-ui-guide/` — the nav item points at nothing designed |
| The legacy app has the behaviour, so removing it is a regression | **13 conversations and 44 messages** is the entire production dataset. Two years of a live platform produced 44 messages |
| Mentor↔mentee contact outside sessions has to happen somewhere | It may already: sessions carry `booking_message`, and email is a channel that exists |

**44 messages is the number worth sitting with.** It is either evidence that
messaging is unwanted, or evidence that the legacy implementation was bad enough
to suppress demand. The data cannot tell you which, and the answer decides
whether M6 is one phase or two.

---

## Register — what blocks

| # | Question | Blocks |
|---|---|---|
| 1 | **Is in-app messaging in scope?** | the whole shape of M6 |
| 2 | **Does a notifications *table* get built?** The bell implies an in-app feed; ADR 0025 built delivery only. If yes it is a build, and D22 is the design; if no, the 681 legacy rows have nowhere to go and M6's notification half is *archive and drop* | the notification half entirely |
| 3 | Are the 220 title-less, body-less notifications migrated, dropped, or archived? | the transform |
| 4 | Does `messageRequestAccept` become a conversation state, and are messages editable? Both exist in legacy and neither is in D29 | the messaging schema, if 1 is yes |
| 5 | Confirm `Session`/`Review` population against production before building typed FKs for them | D22's typed-FK half |

Questions 1 and 2 are the ones to ask first. **3, 4 and 5 are answerable only
after them**, and asking all five at once would be the wrong shape.

---

## Transition notes — what M5b learned by running into it

These cost time in the credit build and are not written down anywhere a new
session would find them.

**Eight registries must be updated when a table or model is added**, each an
explicit list that fails a test when it disagrees:
`TEXT_CHECK_ENUMS` / `UNCONSTRAINED_ENUMS`, `EXPECTED_MODELS`, `models.__all__`,
`APPEND_ONLY`, `EXPECTED_TABLES`, `RETAINED_ON_USER_DELETE`.

**Domain enums must live in `domain/enums.py`.** The registry test partitions
`vars(enums)` **both ways**, so a vocabulary declared elsewhere fails it.

**A `201` route must set `Location`, and every OpenAPI tag needs a description.**
Both are source-walking tests, so neither shows up until the suite runs.

**The ADR lands in the phase's *first* PR, not its last.** ADR 0026 landed in
#197 and ADR 0027 in M5b's PR 1, each amended by later PRs that added a
divergence. Holding it to the end means every intermediate PR cites a record
that does not exist yet.

**Stacked PRs are the norm here and Alembic's chain is linear.** Branch each PR
off the one below, fix `down_revision` at merge time in merge order, and never
`--delete-branch` a squash-merge that is the base of another.

**Every PR runs `/security-review` and `/code-review` before pushing**, on top of
the build gate.

**The gate needs `TEST_DATABASE_URL`** in a fresh worktree — `.env` is gitignored
and does not come across, and without it 1,000+ db tests skip and coverage fails
at ~69% against an 85% threshold. That failure is not the change; it is the
missing DSN.

---

## Where M5 left things

**M5a and M5b are complete.** M5a: #197–#205 plus moderation in #208, #218–#220.
M5b: #209–#223, ADR 0027, three cron workflows.

Two things carried forward, neither a gap:

- **#224 was open at the time of writing** — a configurable credit ladder and the
  first admin grant endpoint. `admin_grant` was explicitly out of M5b's scope, so
  it extends the phase rather than completing it.
- **Neither the reviews nor the credits loader has met production data**, by
  design. That is the rehearsal gate at cutover, not outstanding work.

**Operator prerequisite:** the review and credit template ids must be set in
config or nothing sends — a missing key raises rather than falling back.
