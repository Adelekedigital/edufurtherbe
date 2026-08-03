# 6. Build booking-scoped messaging; keep mentor–mentee conversation on-platform

Date: 2026-08-03

## Status

Accepted

## Context

Mentors and mentees need to communicate around a booking — to agree what the
session will cover, to share a document beforehand, to say they are running late.
Nothing is built for this today and the legacy application is not the source of a
requirement here; this is a decision about what to build, taken now because two
adjacent choices depend on it.

**The first dependency is WhatsApp.** The platform is adopting WhatsApp for
transactional messaging in the phase after cutover. Once a WhatsApp integration
exists, the cheapest possible answer to "how do mentors and mentees talk" is
"they already can". Whether that is acceptable has to be settled before the
integration is built, not after users are using it.

**The second is payments.** Decisions #8 and #11 defer payments, but they are
coming, and decision #9 has mentors setting their own rate. A platform that
mediates paid bookings has a structural interest in where the conversation
happens: the moment both parties hold a direct channel, arranging the next
session privately costs them nothing and costs the platform the booking. This is
the ordinary disintermediation dynamic of any marketplace, and it is a
consequence of the fee, not of anything either party does wrong.

**The requirement is narrower than it first appears.** What a booking needs is a
thread attached to it — participants fixed by the booking, lifetime bounded by
the session, low volume, asynchronous. That is much closer to comments on a
record than to chat. "Chat" — presence, typing indicators, read receipts, media
handling, moderation tooling, mobile SDKs — is a materially larger product, and
buying it means buying all of it.

**Speed to market is a stated priority**, which argues for buying. Volume is not
a constraint at 44 mentors.

Alternatives considered were a hosted chat vendor such as GetStream, using
WhatsApp as the conversation channel, and deferring messaging entirely.

## Decision

**We build booking-scoped message threads.** A thread belongs to a booking. Its
participants are the booking's mentor and mentee. It is ordinary rows in our
Postgres, served by this API.

**We do not buy a chat platform.**

**WhatsApp is bounded to platform-to-user transactional messages** — booking
confirmations, reminders, cancellations, and similar. It is not a channel for
mentor–mentee conversation, and no feature should route a mentor's message to a
mentee over WhatsApp.

This boundary is the load-bearing part of the decision. The build-versus-buy
question is reversible; handing both sides of a paid transaction a direct
off-platform channel is not.

**This is not scheduled work.** It sits after the cutover and after payments in
sequence. The record exists now because the WhatsApp integration lands earlier
and must be built inside the boundary above.

### Rejected alternatives

**GetStream, or an equivalent hosted chat vendor.** A better chat product than we
will build, immediately, with mobile SDKs, moderation tooling and realtime
handled. The strongest argument for it is the stated priority on speed: it would
be live in days rather than weeks, and every hour spent building messaging is an
hour not spent on the migration. It was rejected because the requirement is a
booking-scoped thread rather than chat, the pricing curve steepens sharply once
the free tier is left, and messaging content is exactly the data a marketplace
wants to keep — for moderation, for dispute resolution, and for evidence when a
session is disputed.

**WhatsApp as the conversation channel.** The fastest option and the one with the
best adoption, because it is where this user base already is. In an African
student market that advantage is real and should not be dismissed — a thread in
our web app will be read less often than a WhatsApp message. It was rejected on
disintermediation: it hands both parties a permanent direct line, off-platform,
with no record we can consult and no moderation we can apply. The advantage it
offers is engagement; the cost is the marketplace.

**Defer messaging entirely.** Bookings function with email alone, and the legacy
application has no messaging to preserve. This remains a reasonable position and
is effectively what happens until the work is scheduled. It was not chosen as the
permanent answer because the absence of any in-platform channel is itself
pressure toward WhatsApp — users who cannot message inside the product will
exchange numbers, which produces the rejected outcome by default rather than by
decision.

## Consequences

Message content stays in our database, which makes moderation, dispute
resolution and abuse investigation possible at all. A booking's conversation is
retrievable alongside the booking it belongs to.

Scoping threads to bookings keeps authorization simple: a thread's participants
are derived from the booking, so access is scoped in the query on the booking, in
line with non-negotiable #5, rather than being a separate permission model.

No pricing tier, and no vendor whose per-seat or per-user cost grows with mentor
count.

**We own the build and the maintenance.** Realtime delivery, unread counts,
notification fan-out and attachment handling are all ours. None is hard; together
they are more work than they look, and they will be underestimated.

**There will be no mobile SDK**, no presence, no typing indicators and no read
receipts unless we build each one. Users accustomed to WhatsApp will notice.

**Moderation becomes our problem the moment the feature exists.** Holding the
content is what makes moderation possible, and also what makes it our
responsibility.

**The disintermediation boundary is not actually enforced by this decision.**
Keeping the thread on-platform does not stop a mentor typing a phone number into
it. What the decision buys is that leaving the platform becomes a deliberate act
that we can observe, rather than the default path the product itself provides.
Detecting or discouraging contact-detail exchange is a separate problem and is
not solved here.

**Deferring the build has a cost**, as noted above: until an in-platform channel
exists, users will find their own, and habits formed then are harder to move than
features not yet built.

### Confirmation

- **Mechanical:** no chat-vendor SDK appears in `pyproject.toml`. Buying a chat
  platform would be a visible dependency change.
- **Not mechanical, until there is a vendor to name:** `check_layers.py` blocks
  vendor SDKs by package name in `[tool.check-layers.forbidden-external]`. No
  chat vendor has been chosen — that is the point of this record — so there is no
  name to list, and the import boundary for one is unenforced until there is.
  Whoever adopts a chat SDK adds it to that list in the same change.
- **Mechanical, once built:** thread access is derived from the booking, so an
  authorization test on bookings covers it. A thread reachable without its
  booking would be a test failure rather than a review finding.
- **Not mechanical:** nothing prevents a future feature sending mentor-authored
  content to a mentee over WhatsApp. The boundary is enforced by review against
  this record, and it is the part most likely to erode — it will be proposed as a
  small convenience, and it will look like one.
- **Not mechanical:** nothing detects contact details exchanged inside a thread.

### Open questions

- **Realtime transport** — polling, server-sent events or websockets — is
  undecided and does not need deciding until the work is scheduled.
- **Attachments**, and whether they reuse the storage chosen in ADR 0005.
- **Retention.** How long a booking's thread survives the booking is a policy
  question with privacy consequences, and it interacts with whatever the
  cancellation and refund policy turns out to be.
