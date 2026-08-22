# Build log — M4 integrations, notifications and the session lifecycle

**August 2026.** What was built, what it decided, and what it found. Written for
whoever picks this up next: the PR titles say what shipped, and this says *why
the shape is what it is* and *what is still missing*.

Read `.claude/skills/project-conventions/SKILL.md` for the settled decisions
themselves — this points at them rather than repeating them.

---

## What shipped

Merged, in order:

| PR | |
|---|---|
| #164 | the Google adapter needs a token, not a table — a stale blocker corrected |
| #165 | spike gate window long enough to actually answer |
| #166 | a Daily session has a room, and `/join` hands back a token |
| #168 | the notification outbox — people get told things |
| #170 | a confirmed session gets a calendar event, and loses it when cancelled |
| #172 | reminders fire, and a signed callback is what fires them |
| #173 | a mentor grants free/busy; the token is sealed before it lands |
| #174 | a mentor's own calendar is subtracted from what a mentee may book |
| #177 | a cancelled session tells the mentee it was cancelled |
| #178 | a cancelled hour goes back on the grid; unavailability is an exception |
| #179 | every settled decision gets its own number, and a test keeps it that way |
| #180 | a mentor finds out when their calendar stops working |
| #182 | how email template variables get discovered, resolved and checked |
| #184 | a mentor is told whether their application was approved |

Open at time of writing: **#183** (message variables) → **#185** (session
reminders and feedback) → **#186** (admin alert and the `.env` dedup). That is a
stack; merge in that order.

Several PRs appear twice in the history — the first closed, the second merged.
The stack was squash-merged, which makes a branch cut from it stale the moment
its parent lands: its commits are no longer ancestors of `main` even though the
content is. `git diff origin/main...branch` is the thing to trust; GitHub's file
list lies for a while afterwards.

---

## The session lifecycle is complete

Every stage has an endpoint, and every stage has a message that reaches
somebody:

**book** → answer (accept · decline · withdraw · cancel) → **deadline**
(`respond_by`, reminders, expiry sweep) → **venue** (Daily room or Meet link,
calendar event) → **before** (reminders at 24h and 1h) → **join** (tokenised URL
in a −5/+15 window) → **settle** (completed or no-show) → **after** (feedback,
completed only).

Availability underneath it: declared weekly rules, dated exceptions,
per-offering scheduling windows, and the mentor's own Google free/busy.

**What is not session work:** the Daily webhook, self-signup, onboarding,
payments.

---

## Decisions taken, and why

**Free/busy fails open** (ADR 0004's open question, resolved and amended in
place). That record calls the read *advisory* and says it must never be what
prevents a double booking — so an advisory check that refuses a booking when
Google is slow has been promoted to authoritative. An outage degrades to
declared availability, which is exactly the position every unconnected mentor is
in permanently.

**A cancelled hour goes back on the grid**, reversing settled decision #118.
That one exempted `cancelled` on the reasoning that a mentor who cancels is
probably busy. Both halves were true and neither made the hour occupied. What it
produced was an hour hidden from the grid that the exclusion constraint would
have accepted a booking for anyway, with nothing able to release it.
Unavailability is now an **availability exception** — the mechanism that already
means it, which the mentor can see and delete.

**The template declares its variables; the code resolves them.** Nine Loops
templates share about twenty distinct names, so a registry keyed by name beats a
field list per template, and aliases let two disagreeing generations of template
work untouched — which removes the editing pass that would otherwise block the
first send.

**Two Daily gates, both enforced** — measured in a browser, not assumed. A
private room refuses an untokened visitor; `nbf` refuses an early one. Different
refusals, so two things were tested rather than one twice. Consequence: early
joining is *impossible* on Daily and only *discouraged* on Meet, so **the two
venues cannot share UI copy**.

**Reminders are never cancelled.** Scheduled ahead, the callback re-reads, and
one for a session no longer happening does nothing. The alternative makes four
transitions responsible for unscheduling, and the bug is the one somebody
forgets.

---

## Defects found, and what found them

The useful part of this log. Every one shipped-looking and quiet.

| what | how it was found |
|---|---|
| a cancelled hour was hidden from the grid and unguarded in the database, permanently — mentee-drivable | reading `LIVE_STATUSES` against `_busy` after a question about it |
| every message was sent with `dataVariables` almost empty; Loops would have delivered real emails with every field blank | the user pasting the actual templates |
| a reminder to two people queued **one** row — the dedup index had no recipient | a test asserting two |
| the dead-grant record never survived: written on a read-path session that never commits | a test asserting the second request makes no call |
| `_google_account` read an `id_token` Google never sends under a `freebusy`-only scope | reviewing before shipping |
| `create_event` notified on invitation, `cancel_event` said nothing on cancellation | a question about cancellation |
| `.env.example` defined three keys twice; last wins and the last was empty | the user noticing |
| a dangling alias pointing at a resolver that did not exist | the invariant test written for it |
| five shipped docstrings claiming things were unbuilt that were built | found by accident, one at a time |

**The sweep is not automatable, and that is worth recording.** Grepping
`not built`, `unbuilt`, `does not exist` and `still open` across `src/` returned
about forty hits at roughly 1-in-20 signal: most were `"does not exist"`
describing a **404 response**, which is the same words about something else
entirely.

Worse, one near-miss would have become a wrong "fix" if the pattern had been
trusted. `models/sessions.py` says *"Nothing reads it yet"* of
`SessionType.requires_booking_confirmation` — and booking *does* read a column
of that name, on `SessionTypeBookingConfig`, a different table. Two same-named
columns, one read and one not. The comment is correct.

So there is no lint rule to add here. What the codebase gains is the habit of
checking a claim when touching the code it describes, which is the only thing
that would have caught any of these.

**The pattern worth naming:** four of these were *guards that another guard
already covered*, or *text that was true when written*. Neither shows up in a
passing suite. Settled decision #157 records the first shape; the second has no
gate at all, which is the next thing worth building.

---

## What is still missing

**Blocking real use — yours, not the code's:**

- **Credentials.** `GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET`, `CALENDAR_TOKEN_KEY`,
  `APP_BASE_URL`, `LOOPS_API_KEY`, `EMAIL_TEMPLATES`, `DAILY_API_KEY`, the QStash
  values. Everything is built and inert without them.
- **Four templates do not exist in Loops**: `request_expired`,
  `calendar_disconnected`, `mentor_approved`, `mentor_declined`. The last three
  are not session messages and need only the name fields.

**Unbuilt, in the order I would take them:**

1. **Self-signup.** Accounts come from `scripts/provision_auth.py`, which is
   right for the cutover (ADR 0018, eager provisioning) and leaves every new
   mentee after it an operator ticket. Supabase mints an identity; nothing
   creates the matching `users` row, so `get_current_user` answers `404`. Needs
   a decision first: auto-provision on first authenticated request, or an
   explicit registration endpoint.
2. **The Daily webhook.** `participant.joined` and `participant.left`, which are
   what make a *waiting* screen honest — today the arrival a party sees is our
   own Join press, so somebody who presses it and never reaches the room leaves
   the other looking at a lie. Unblocked; Q1 and Q2 are answered.
3. **Onboarding.** `user_onboarding` exists from M1, written only by the ETL.
   Nothing in the application reads or writes it.
4. ~~**A stale-claim sweep.**~~ **Done.** Seven found in total; the sweep
   turned up two more beyond the five found by accident.

**Deliberately not touched:** `docs/handoff-session-build.md`. A live session
holds a worktree writing `docs/handoff-review-build.md`, and that file is very
likely being rewritten. It carries at least one stale reference — `NEVER_AGREED`,
a constant that no longer exists.
