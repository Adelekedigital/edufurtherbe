# 10. One chat per mentor and mentee pair

Date: 2026-10-27

## Status

Accepted.

Answers the question ADR 0007 reserved this number for ("Message thread scope:
booking-scoped vs standalone"). **Supersedes ADR 0006 on one point only**: "a
thread belongs to a booking". ADR 0006's other decisions stand unchanged: we
build messaging in house rather than buying a chat platform, and **WhatsApp
stays bounded to platform-to-user messages**, so no mentor-written text ever
goes out over WhatsApp.

Lands `Accepted` in the pull request that implements it (the schema), as
ADRs 0011 and 0013 did.

## Context

The Messages PRD (founder decisions, 2026-10-26) ships messaging for Nov 15 as
one chat between a mentor and a mentee, reached from the inbox, from a booking,
from Session Join and from the mentor's profile. The design draws one chat per
person, with the next booking shown in its header.

ADR 0006 chose a thread per booking because its participants are fixed by the
booking and its access is "derived from the booking, scoped in the query".
Both reasons survive a chat per pair, as long as access still comes from
bookings. A thread per booking does not survive the design: a mentee who books
the same mentor three times would hold three threads with one person, and the
conversation that carries from one session to the next is the one people
actually have.

The migration package (`07_communications.sql`) has no booking key on
`conversations` either. It keeps a chat as rows of its own with participants,
which is the shape this needs.

## Decision

**A chat belongs to the two people, not to a booking.**

1. `conversations` names `mentor_user_id` and `mentee_user_id` and is **unique
   on the unordered pair** (`uq_conversations_pair`, on `LEAST`/`GREATEST`):
   two people have one chat, whichever of them is the mentor. The roles are
   the ones the pair had when the chat began.
2. **Access is still a booking, checked in the query** (M0, non-negotiable
   #5): two people may read or write their chat only while one is the mentor
   and the other the mentee of at least one session that was booked
   (`confirmed`, `completed`, `no_show` or `cancelled`; a cancelled session was
   confirmed first). A pending, declined, expired or withdrawn request is not a
   connection. No grant is stored: a chat is unreachable without a booking,
   and a pair that books later sees the history it already had.
3. **Membership is the pair columns.** `conversation_participants` holds each
   person's read state (`last_read_at`, forward only) and nothing else, so it
   is never a second answer to who is in the chat.
4. **The package's trigger and `last_message_preview` are dropped.** The
   preview depends on who reads it ("You: …"), so it is built on read;
   `last_message_at` is written by the send, in the message's own transaction.
5. **A retried send lands once**: `messages.client_id` is unique per sender,
   in the spirit of ADR 0024. A file is uploaded first and attached on send
   (`message_attachments.message_id` is null until then).

### Rejected alternatives

**Keep a thread per booking (ADR 0006).** Rejected above: it multiplies the
threads with one person and splits one conversation by session.

**A chat per pair, unique on the ordered `(mentor_user_id, mentee_user_id)`.**
A mentor may book another mentor, so two people can be connected in both
directions. Ordered uniqueness would then allow two chats between the same two
people, and every endpoint keyed by the other person's id would have two
answers.

**Store a grant on the chat when a booking is accepted.** A second copy of a
fact `sessions` already holds (non-negotiable #8), which would need writing on
every transition and could disagree with it.

## Consequences

The inbox lists people rather than bookings, and a chat outlives any one
session. Legacy chats (13 conversations, 44 messages) import as they are (an
imported message is exempt from the body's length CHECKs, and two legacy
chats for the same two people merge into one), and
an unconnected pair's chat stays stored and hidden until they book.

A person who closes their account keeps their messages in the other person's
chat (the closed-chat line: "You can still read your past messages"), so a
sender restricts rather than cascades (ADR 0013) and the account purge leaves
messages as it leaves sessions. Their name goes from every read, as it does
for a deleted session party.

Moderation is still ours (ADR 0006), and support reads a chat in the database
until an admin view exists.

### Confirmation

- **Mechanical:** `uq_conversations_pair` refuses the same two people in either
  order; `uq_messages_sender_client` refuses a repeated `client_id`
  (`tests/integration/test_migrations_messages.py`).
- **Mechanical, with the endpoints:** every read and write is tested with a
  third user, connected to neither person, who can read and send nothing, and
  with each session status in and out of M0.
- **Not mechanical:** nothing stops a future feature sending a message's text
  over WhatsApp. ADR 0006's boundary is still enforced by review.
- **Not mechanical:** nothing detects contact details exchanged in a chat. The
  composer's notice discourages it; it does not prevent it.
