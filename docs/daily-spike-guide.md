# Running the Daily spike

`scripts/daily_spike.py` measures six behaviours the meeting-room port is about
to depend on. It is written the way `calendar_spike.py` was, and for the same
reason: **the Google side surprised us twice, and neither surprise was in the
documentation's foreground.** `conferenceDataVersion=1` silently dropped the
conference, and free/busy read back empty right after a write. Both were found
by measuring and would have been missed by reading.

Nothing is decided by running this. It prints what happened.

## Measured results

Run against a real account on 2026-08-19, room `efspike-4baae5dedb`.

| Question | Answer |
|---|---|
| Q1 `privacy: private` refuses a visitor holding the URL and no token | **not reported** — see below; every session room is private regardless |
| Q2 a token's `nbf` refuses an early join | **not reported** |
| Q3 the meeting record gives per-participant **join and leave** | **yes** — `join_time` plus `duration` |
| Q4 the `user_id` minted onto the token reaches that record | **yes**, verbatim |
| **Q5 how long after the call the record appears** | **it does not wait** — readable *during* the call |
| Q6 `exp` closes the room, and what somebody inside sees | **not reported** |

**Q5 is the opposite of the Google result, and that is the headline.** Free/busy
read back empty right after a write; Daily's record was there mid-call, with
`"ongoing": true`. There is no lag to design around.

**But `ongoing` introduces a different trap, and it is the one to plan for.** A
record read while the call is running carries *partial* durations — the second
read of the same meeting returned identical numbers with `"ongoing": false`, so
the finalised values only exist once everybody has gone. **The join window shuts
fifteen minutes after the start, and a session runs thirty to ninety** — so at
the moment the current settlement fires, the meeting is still in progress.

**Q3 and Q4, measured:**

```
room opened (nbf)   02:34:26
mentee  joined 02:35:52  left 02:38:09  present 137s   user_id spike-mentee-55e2d5c4
mentor  joined 02:35:59  left 02:37:09  present  70s   user_id spike-mentor-9ae25809

co-presence: 02:35:59 -> 02:37:09 = 70s
```

Two intervals, overlapping by 70 seconds, each attributed to the id we minted.
Neither is a boundary case — the mentor arrived after the mentee and left before
them — so the record genuinely distinguishes them rather than reporting one
meeting-level span twice.

There is no explicit `leave_time`, and **that is Daily's design rather than an
omission in this endpoint** — their documentation says the same of the
`participant.left` webhook, which carries `joined_at` and `duration` and no
leave timestamp. `joined_at + duration` is the sanctioned derivation on both
transports, so it is one definition rather than a workaround, and it has one
fewer clock to disagree with itself.

The webhook additionally carries `event_ts`, documented as *close to but not
exactly* when the participant left — so even there the arithmetic is the
authority and the timestamp is not. That matters for the live path below: the
event tells you *that* somebody left, and `joined_at + duration` tells you when.

### The two transports are for two different jobs

Both carry the same fields, so an earlier draft of this guide concluded we do not
need the webhook at all. **That was wrong, and wrong in a specific way: it
analysed settlement and stated the answer as though it covered everything.**

| job | when | transport | why |
|---|---|---|---|
| **settle** — `completed` against `no_show` | after the session | `GET /meetings` | the sweep already runs; the record accumulates every participant, so nothing is missed between polls |
| **show who is in the room** | during the session | `participant.left` / `participant.joined` webhook | polling at any tolerable rate is either laggy or expensive, and it is the *other* party who is waiting |

**Settlement genuinely does not need a push.** The record aggregates, so a
participant who joined and left between two polls is still in it — the hourly
sweep loses nobody, and Q5 means the data is there when it asks.

**Live attendance does.** The waiting screen built on `join_opens_at`,
`join_closes_at` and each party's arrival is currently fed by our **own** Join
button, which records an *intention*: a mentee who presses it and never reaches
the room leaves the mentor looking at "your mentee has joined" while they sit
alone. Daily's events are the only thing that makes that screen true, and
"true within a second" is not something a poll can deliver without either a
tight interval per live session or a stale display.

So the webhook is a real component of the Daily phase, not an alternative to the
pull. What it costs, stated so it is planned rather than discovered: a public
endpoint, signature verification, and tolerance for retries and out-of-order
delivery — a webhook that assumes exactly-once and in-order is a webhook that
records a leave before the join it belongs to.

## What this changes

**Co-presence is computable, so `completed` can mean the session happened.** The
option this project deferred — *does `completed` require overlap* — is no longer
blocked by "we cannot see it anywhere". It is answerable on Daily and remains
unanswerable on Google Meet, which is the argument for Daily as the default.

**Attendance settlement has to split into two questions asked at two times.**

| question | answerable at | source |
|---|---|---|
| did each party turn up | `starts_at + 15m`, when the join window shuts | the platform's own `/join`, or the record |
| were they ever in the room together | after the session ends, when `ongoing` goes false | the record |

The current sweep answers both at the window's close, which is correct for the
first and impossible for the second. Either it settles later, or attendance
upgrades from `reported` to `observed` in a second pass — and
`session_events.metadata` already carries that distinction for exactly this
reason.

**`session_participants.left_at` is writable**, from `join_time + duration`.
That is the producer `left_early` was kept off the droppable list for.

**The token's `user_id` must be our `users.id`.** It round-trips verbatim, so
attendance is attributable without a lookup table.

## Answered 2026-08-21, in a browser

| question | result | Daily's words |
|---|---|---|
| **Q1** — bare room URL, no `?t=` | **not admitted** | *"You are not allowed to join this meeting"* |
| **Q2** — mentor URL, token and all, before `nbf` | **refused for being early** | *"This meeting is not ready yet"* |

**Two different refusals**, which is what makes this two measurements rather
than one repeated. *No token* and *too early* are distinct gates and both hold.

**Q1 makes the promise real.** "Nobody can join without the link we gave you" is
now something the platform can say rather than hope — and a promise nobody
checked is the kind that surfaces in a complaint.

**Q2 resolved toward enforced, which is the stronger branch.** Early joining is
*impossible* on Daily and only *discouraged* on Meet, so the two venues are not
equivalent and **their UI copy cannot be either**. A Daily session has two
layers — the link is withheld until the window and the door is shut. A Meet
session has one, because a Meet link admits anybody holding it at any time,
which is why links are withheld rather than sent in advance.

Both `nbf`s are set together, one on the room and one on the token, so this
tested the pair. Which of the two refuses is unknown and does not matter while
both are set; the guide below explains how to separate them if that ever
changes.

### How they were answered, and how to repeat it

Both are browser observations the script cannot make, and **two minutes was too
short** — which is why they went unanswered on the first run. Give yourself
room:

```bash
uv run python scripts/daily_spike.py --opens-in 10
```

The room then opens ten minutes after creation, and the script prints the exact
instant plus a countdown. Before it:

1. **Q1** — paste the **bare** room URL, with no `?t=` on the end. Expected: you
   are not admitted. If you land in the room, `privacy: private` is not doing
   what the design assumes.
2. **Q2** — paste the **mentor** URL, token and all. Expected: refused for being
   early. If you land in the room, `nbf` is metadata rather than a gate.

**If both work they refuse for different reasons** — *no token* against *too
early* — and that difference is how you know you tested two things rather than
one thing twice. Note the wording of each; it is what the product's copy has to
be honest about.

After it opens, join in two browsers for Q3–Q5 as before, then:

```bash
uv run python scripts/daily_spike.py --cleanup-only
```

Both `nbf`s are set together — one on the room, one on the token — so a single
run tests them as a pair. Telling them apart needs a second run with one of them
removed, and is only worth doing if the pair turns out not to refuse.

## Why each question is here

**Q1 — the withheld link rests entirely on it.** The whole design for Daily is
that the room URL is never published: the calendar event is a hold, and the link
reaches the participant only through the platform at join time. If a bare URL
admits somebody, that design is decoration and the link may as well go on the
invitation.

**Q2 — this is the time gate.** Product wants participants not joining before
the session. On Google Meet that can only ever be a request in the UI copy; on
Daily `nbf` should make it a refusal at the room. If it turns out to be advisory,
Daily and Meet are equivalent here and the "Daily is the provider where gating
is real" argument collapses.

**Q3 — presence is not attendance.** One timestamp per person says they arrived;
it does not say the two were ever in the room together. The near-miss case —
mentee joins at +1 and gives up at +10, mentor joins at +12 — is invisible
without both ends, and it is the case that makes `completed` a lie.

**Q4 — unattributable times are no better than none.** Per-participant figures,
and eventually per-participant *payouts*, need the record to name which of our
users each interval belongs to.

**Q5 — the one that decides the architecture.** The settlement sweep runs on a
schedule. If the meeting record lands minutes after the call, a sweep firing on
time reads an empty record and settles a session that happened as `no_show` —
and nothing can undo it, because joining after the window shuts is refused. This
is exactly the shape of the free/busy indexing lag, and that one was invisible
until somebody timed it.

If the answer is "several minutes", the sweep needs to lag the join window by
more than the settlement boundary, and that number comes from here.

**Q6 — the end of the window.** Mostly to know what a participant experiences,
so the UI can say something true about it.

## Before you run it

1. **An API key.** Daily dashboard → Developers → API key. Put it in
   `script-data-dev/daily_api_key.txt`, one line, no quotes. A file rather than
   an environment variable, following the calendar spike, so non-negotiable #6
   holds everywhere and the directory's existing ignore rule covers it.
2. **Two browsers**, or one plus a private window. The two participants must be
   separate sessions or Daily sees one.
3. **About fifteen minutes.** The room opens two minutes after creation so Q2
   has something to refuse, and Q5 polls for up to ten.

## Running it

```bash
uv run python scripts/daily_spike.py
```

No `--with` flags: it speaks plain HTTP and `httpx` is already a project
dependency.

The script creates the room, prints the two tokenised URLs and what to do with
them, and waits. **Leave at different times** — a minute apart is plenty. Two
identical intervals cannot tell you whether the record distinguishes them, which
is the whole of Q3.

Note the wall-clock time you left each one. A record that exists but disagrees
with what you observed is worse than no record, and you cannot notice that
without having watched.

Then:

```bash
# read the record again without recreating anything
uv run python scripts/daily_spike.py --measure-only

# delete the room
uv run python scripts/daily_spike.py --cleanup-only
```

## Answering the questions the script cannot

Q1, Q2 and Q6 happen in a browser and the script never sees them. It prints what
to try and when; you write down what happened. That split is deliberate — a
script that reported its own interpretation of a browser it cannot see would be
inventing the answer.

## What each result means

**Q1 no** — the withheld-link design is off for Daily as well as Meet, and both
providers publish their link. The time gate is then the only lever left, so Q2
becomes decisive rather than merely useful.

**Q2 no** — "do not join early" is UI copy on every provider. Daily keeps its
advantage on attendance and loses it on gating, which is worth knowing before
anybody promises a mentor that the room is shut.

**Q3 no**, or join-only — co-presence is not computable even on Daily, and
`completed` cannot mean *the session took place* anywhere. That would settle the
open question by removing the option, and the honest answer becomes recording
self-report and saying so.

**Q4 no** — attendance is aggregate rather than per-person. `session_participants`
could not be written from this, and the mentee attendance rate stays self-report.

**Q5 slow** — the settlement sweep cannot read attendance at the join window's
close. Either the sweep runs later than the boundary, or attendance settles in a
second pass behind the status. Either is fine; picking without the number is a
guess.

**Q6 anything surprising** — worth a line in the port's docstring so a mentor
running over is not a support ticket nobody can explain.
