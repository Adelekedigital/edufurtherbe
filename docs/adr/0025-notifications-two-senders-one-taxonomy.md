# 25. Notifications: two senders, one taxonomy, and scheduled callbacks

Date: 2026-08-19

## Status

Accepted.

Has **no counterpart in `docs/edufurther-migration/`** rather than contradicting
one — the canonical package specifies no notification system at all. ADR 0007's
authority is silent here, so this record is where the shape is decided.

Constrains, and is constrained by, ADR 0006 (WhatsApp is platform-to-user
transactional only) and settled decisions #13 (no platform-native queue or cron)
and #16 (push is native Web Push).

## Context

Nothing in this service sends anything to anybody. The ports exist and refuse
loudly; the vocabulary has one member. Meanwhile the lifecycle that would use
them is complete: booking, four transitions, expiry and attendance settlement
all write events today, and a mentor with an unanswered request is told nothing
until they happen to look.

Three facts shaped what follows, and none of them is a preference.

**The templates are in Loops and cannot be exported.** Rebuilding them elsewhere
is manual work nobody has time for now.

**Supabase already sends the auth code through Emailit's SMTP**, on a warmed
domain. Settled decision #12 chose Supabase partly because passwordless login
"arrives without being built" — so the highest-stakes message in the product is
already routed, and not through the port this record is about.

**The reminder schedule outgrew the scheduler.** Settled decision #122 fixed
three sends per pending request; the join reminders the product wants are 24
hours, 30 minutes and 5 minutes before a session. The hourly sweep in
`settle_sessions` cannot deliver the last two, and GitHub Actions schedules are
documented as delayable under load — a 5-minute reminder could fire after the
session started.

## Decision

### 1. Two email senders, split by use case

| sender | carries |
|---|---|
| **Emailit** | the Supabase auth code (SMTP), marketing, and list subscription |
| **Loops** | every transactional message this service sends |

**This is not the split a clean sheet would produce**, and recording why matters
more than the split itself. Auth is transactional by every definition that
counts, so the arrangement puts the one message that must arrive alongside
*marketing* — the class that generates complaints — and separates it from the
transactional mail it most resembles.

It is chosen anyway because the alternative is rebuilding every template against
a deadline that has migration work in front of it, and because Emailit is a
lifetime subscription already warmed and already carrying auth. **A constraint
honestly stated beats a principle that does not survive contact with the work.**

### 2. Sending subdomains, not just senders, separate the reputations

**This is the part that makes the split above safe, and without it the split
does nothing.** Reputation at a mailbox provider attaches to the `From:` domain
and its DKIM signature, not to which vendor relayed the message. Two senders on
one domain share one reputation, so a bad campaign degrades the deliverability
of a login code sent by the other.

So: marketing, transactional and auth each send from their own subdomain, each
with its own DKIM key and DMARC alignment. The split is a deliverability
decision, and a deliverability decision that shares a domain is decoration.

### 3. Template ids carry their provider

```
mentor_response_reminder = "loops:tmpl_abc"
```

The map in `core/config.py` is keyed per message, so prefixing the value with
the sender makes moving one message between providers **a single string in
configuration** — no code change, no deploy of the adapter, no all-or-nothing
migration. That is the incremental path out of Loops if it is ever wanted, and
it costs nothing to build in now.

### 4. Who gets told: one rule, one exception

> **A message caused by somebody goes to the party who did not cause it. A
> message caused by time going past goes to both.**

| event | told |
|---|---|
| booked on an auto-confirming offering | the **mentor** — the mentee is looking at a confirmation screen |
| mentor accepts | the **mentee** — they have been waiting on an answer |
| mentor declines | the mentee |
| mentee withdraws | the mentor |
| either party cancels | the other one |
| **request expires** | **both** — nobody acted |
| session reminders | both |

**The two confirmations are the same rule applied twice**, not two rules, and
their content differs: the mentee's carries the join link and the calendar hold,
the mentor's says a booking arrived. Sending both parties both — which the
legacy app did — tells each of them something they already know.

`expired` is the principled exception rather than an oversight: it is the only
outcome no person produced.

### 5. Reminders are scheduled callbacks, and the callback re-checks

Reminders are published to **QStash** when the session or request is created,
not selected by a sweep. Per-session scheduling is precise where polling is only
as good as its interval, and QStash is vendor-neutral HTTP, so settled decision
#13's exit criterion — no *platform-native* queue — is untouched. It is already
in `pyproject.toml`'s vendor allowlist.

**The callback re-reads the session before it sends anything.** That is the
whole design. Scheduling ahead normally obliges you to cancel: a session called
off must not still announce itself five minutes before a start that is no longer
happening, which means storing message ids and unscheduling them on four
different transitions. Re-checking at delivery recovers the sweep's
self-correcting property — **nothing is ever cancelled, because a callback for a
dead session simply does nothing** — and the cost is a handful of wasted
callbacks.

### 6. The 5-minute nudge is deferred to the front end

Settled decision #16 already chose native Web Push, and "starting in 5 minutes"
is what push is for. It is a front-end conversation and is not blocked on
anything here; the 24-hour email stands alone.

## Consequences

**Auth is outside this port entirely.** Supabase sends it, so the `Notifier`
never carries the message the product can least afford to lose. That is a
comfort and a blind spot at once: nothing in this repository tests that login
mail arrives, and nothing here would notice if it stopped.

**Two senders is two of everything operationally** — two dashboards, two sets of
DNS records, two bounce streams, two suppression lists. A recipient who
unsubscribes from marketing at Emailit is not suppressed at Loops, which is
correct for transactional mail and is exactly the kind of thing that looks like
a bug when somebody reports it.

**Every WhatsApp message needs a Meta-approved template**, because every message
here is business-initiated and therefore outside the 24-hour service window.
That approval has a lead time nobody in this project controls, which is the
strongest argument for template ids living in configuration.

**A public callback endpoint arrives with this**, needing signature
verification, replay tolerance and out-of-order tolerance — the same machinery
the Daily attendance webhook needs. Building them together is cheaper than
building them apart.

### Confirmation

Nothing here is built; this record is the shape, and what follows is what the
build has to demonstrate.

| claim | what will check it |
|---|---|
| the audience rule holds | one test per event asserting *who was told*, and — more importantly — **who was not** |
| expiry tells both | its own test, because it is the rule's only exception and an implementation following the rule mechanically would tell nobody |
| a callback for a dead session sends nothing | cancel a session with a reminder pending, fire the callback, assert silence |
| a message moves provider by configuration | resolve one notification under each prefix and assert the adapter chosen |
| a missing template fails loudly | already covered — `ConfigurationError`, not a silent no-op |

**Blind spots, stated because they are the parts most likely to be discovered
rather than remembered.**

- **Nothing tests that auth mail arrives.** It is Supabase's SMTP, configured in
  a console this repository cannot read, and the first symptom of it breaking is
  1,200 people unable to log in during cutover.
- **The subdomain separation is a DNS fact, not a code fact.** No test in this
  repository can assert it. It will be true because somebody configured it, and
  false the day somebody changes a `From:` address.
- **Suppression is per provider and nothing reconciles them.** A hard bounce at
  Loops does not stop Emailit sending to the same address.
- **Volume is untested against the Loops free tier.** 4,000 sends per rolling 30
  days is comfortable at the measured booking rate, and the free tier's
  1,000-contact cap does not apply to transactional-only recipients — but it
  applies the moment any of those 1,200 people receive marketing from Loops too.

## Alternatives considered

**One sender for everything.** The arrangement a clean sheet produces, and it
was recommended twice in the discussion that produced this record. Rejected on a
constraint rather than an argument: the templates exist in Loops and cannot be
exported, and rebuilding them by hand competes with migration work. Section 3
keeps the door open a message at a time.

**Auth with transactional, marketing alone.** The pairing that separates the
right things. Rejected because auth is already routed through Emailit's SMTP on
a warmed domain, and moving it to buy a tidier split would put the migration's
most fragile moment on a cold reputation to gain nothing this quarter.

**A tighter cron instead of QStash.** Rejected twice over: GitHub Actions
schedules are delayable under load, so a 5-minute reminder is unreliable however
short the interval, and a sweep that runs every few minutes scans the whole
table to find almost nothing.

**QStash with cancellation on transition.** The obvious reading, and it makes
four transitions responsible for unscheduling — so the bug is a message that
fires for a session called off through the one path somebody forgot. Re-checking
at delivery makes that state unreachable rather than merely handled.
