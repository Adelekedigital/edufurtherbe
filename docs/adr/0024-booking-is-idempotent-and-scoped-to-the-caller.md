# 24. Booking is idempotent, and the key is scoped to the caller

Date: 2026-08-18

## Status

Accepted.

Diverges from `docs/edufurther-migration/` on one column, which under ADR 0007
is what makes this a record rather than an implementation note. The package
specifies `idempotency_keys.key text PRIMARY KEY`; this ships `id uuid` with
`UNIQUE (user_id, key)`.

## Context

`POST /api/v1/sessions` is the first write to `sessions`. A booking is not
naturally repeatable: sending it twice books two hours, and after payments it
charges twice. The canonical package anticipated this and specifies the table —
its own comment names the failure as *a flaky mobile connection retrying a
booking*.

That is the shape of the problem here, and it matters for what follows. The
retry is a phone on a bad connection replaying one request, **not** two people
racing for one slot. The two look alike and need different mechanisms.

## Decision

**Four things, and the third is the divergence.**

### 1. `Idempotency-Key` is required, not recommended

Stripe treats the header as optional, and can: its callers are servers written
once by someone reading the documentation. The caller here is a mobile client on
a connection that drops mid-request, and an optional header makes the guarantee
opt-in for exactly the caller who most needs it.

Requiring it is also the safe direction to be wrong in. Relaxing a required
header later breaks nobody; requiring an optional one breaks every client that
had been omitting it.

### 2. The table is a replay cache in front of a constraint, not the control

What makes a double booking impossible is `sessions_no_mentor_double_booking` —
two bookings for one mentor at one instant overlap by definition, so the second
is refused whether or not a key was sent. `idempotency_keys` does something
narrower: it turns that refusal into *the original answer* for the client whose
connection dropped.

This is stated as a decision because the opposite belief is what makes an
idempotency table dangerous. A reader who thinks the table is the control will
eventually relax the constraint behind it, and the two failures they were
guarding against are not the same failure.

### 3. The unique is `(user_id, key)`, not `(key)`

The package makes `key` the primary key, so the key space is global. That cannot
survive contact with the requirement above it: a key row holds a stored
**response body**, so the lookup must be scoped to the caller or one user is
served another user's booking.

Once the read is scoped, a global key space is not a stricter rule — it is a
defect. User B sending a key user A already holds finds nothing on the scoped
select and then collides on the insert: a failure with no correct answer to
give. Stripe scopes per account, and for this reason.

`NULLS NOT DISTINCT`, because the package leaves `user_id` nullable for an
idempotent endpoint that takes no token. Under the default two anonymous
requests sharing a key would both insert, which is the one thing the table
exists to prevent.

The surrogate `id` is the second, smaller divergence and needs no argument here:
ADR 0015 admits no exception, non-negotiable #10 prescribes exactly this
resolution, and the build plan recorded it in advance.

### 4. Legality is asked of `/slots`, not re-derived

`starts_at` must be an instant the slot grid currently offers, to the second.
The endpoint calls `list_slots` and tests membership rather than re-implementing
the notice window, the mentor's hours, the offering's own scheduling windows,
blocked dates, and existing bookings.

## Consequences

**The refusals collapse deliberately.** Too soon, outside the mentor's hours, on
a blocked date, and already taken are all *this instant is not offered*, and a
client's only correct response to each is to re-read `/slots`. Distinguishing
them would publish four reasons a client cannot act on differently.

**A refused booking leaves nothing behind.** The reservation and the session are
one transaction, so a refusal rolls both back. The alternative — committing the
reservation first — would poison that key permanently: every retry would be told
a request is in flight, forever, from one unlucky attempt.

**The sequential double booking is a `422`, not a `409`.** By the time the second
mentee asks, the slot genuinely is off the grid. The `409` belongs to the race,
where both read the grid before either wrote — which is the case the pre-check
cannot cover and the constraint exists for.

**Expiry is enforced by the queries.** A row past `expires_at` is invisible to
the lookup and is reclaimed in place by the next reservation for that key. The
retention job in the package's runbook still earns its place — it keeps the
table small — but nothing depends on it having run.

### Confirmation

| claim | what checks it |
|---|---|
| a retry replays rather than rebooks | same key twice → one session, identical body, `Idempotent-Replayed: true` |
| the key is scoped | two callers share a literal key; the second **retries**, and gets their own booking back |
| a different body is refused | same key, changed `topic` → `422` naming the cause |
| the header is required | omitted → `422`, no session written |
| an expired key is reclaimed | `expires_at` moved into the past; the same key books again |
| legality comes from the grid | an instant thirty minutes off the grid → `422` |
| the constraint refuses a race | two open transactions, the loser blocked on the lock, then `409` |
| a refusal is not sticky | refused attempt, then the same key succeeds |
| confirmation resolves config → mentor | three tests: inherit, override, and neither |

Every one of those was watch-failed. Five deliberate mutations were run and each
was caught by the intended test — with one exception worth recording: **the
scoped lookup survived its mutation the first time.** With one booking each, the
insert never conflicts, so the select is never reached; the test only became a
test when the second caller retried. That is the finding this record would most
want a future reader to know.

**Blind spots.**

- **Nothing writes the mentor's answer yet.** A booking that lands
  `pending_mentor_approval` cannot be accepted or declined, and nothing expires
  it — that is the next release, and until it ships such a booking holds the
  slot indefinitely.
- **No intake answers are captured at booking.** `intake_submissions` requires a
  `session_id`, so it is now reachable for the first time; the surface is not
  built.
- **The 24-hour TTL is not configurable** and is written in two places that must
  agree — the column default and the reclaim path. They are one constant in the
  code, not one in the database.

## Alternatives considered

**Optional header, as Stripe has it.** Rejected on who the client is; see above.

**Storing the request body rather than a hash.** A second copy of user input with
its own retention question, and a booking message is something a mentee wrote to
their mentor. The only question ever asked of a stored request is *is it the
same one*, which a hash answers.

**`INSERT ... ON CONFLICT DO NOTHING` for the reservation.** Rejected on the case
that matters: against a concurrent uncommitted conflicting row, `DO NOTHING` may
return immediately having seen nothing, while `DO UPDATE` waits. Waiting is what
lets the second request read the first's committed answer and replay it, rather
than being told the key is free and booking again.

**Re-deriving slot legality in the writer**, so booking does not depend on the
public read path. Rejected as non-negotiable #8 in its most expensive form: the
copy that drifts silently offers or refuses the wrong hour, and the two would be
tested apart so neither test would notice.
