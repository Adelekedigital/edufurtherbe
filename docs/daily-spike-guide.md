# Running the Daily spike

`scripts/daily_spike.py` measures six behaviours the meeting-room port is about
to depend on. It is written the way `calendar_spike.py` was, and for the same
reason: **the Google side surprised us twice, and neither surprise was in the
documentation's foreground.** `conferenceDataVersion=1` silently dropped the
conference, and free/busy read back empty right after a write. Both were found
by measuring and would have been missed by reading.

Nothing is decided by running this. It prints what happened.

## Measured results

*Not yet run.* Fill this table in from a real account, the way the calendar
guide's table was filled in, and the port is built against these answers rather
than against Daily's documentation.

| Question | Answer |
|---|---|
| Q1 `privacy: private` refuses a visitor holding the URL and no token | |
| Q2 a token's `nbf` refuses an early join | |
| Q3 the meeting record gives per-participant **join and leave** | |
| Q4 the `user_id` minted onto the token reaches that record | |
| **Q5 how long after the call the record appears** | |
| Q6 `exp` closes the room, and what somebody inside sees | |

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
