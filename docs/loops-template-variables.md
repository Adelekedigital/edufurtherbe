# Email template variables — discovery, resolution, and the names

**Status: a plan, not a description.** Nothing sends these yet. Today every
message goes out with `dataVariables` almost empty — `session_booked` sends `{}`
— so a configured deployment would deliver real emails with every merge field
blank. No error, no failed row, just a bad email. That is the gap this closes.

**The template declares what it needs; the code supplies it.** Loops publishes
each template's merge fields, so the list of required variables is not duplicated
in this repository at all. What lives here is one **resolver per variable name**
— how to compute `sessionDate` from a session, once, for every template that
asks for it.

---

## Why this shape

The obvious alternative is a field list per template: "Session Confirmation needs
these eight." It works, and it costs a code change every time a template gains a
field — which is exactly the deploy-free flexibility this project wanted.

Counting decides it. Nine templates × ~10 fields is ninety slots, but only about
**twenty distinct names**, because `sessionDate` appears in nearly every one. A
registry keyed by name is a quarter of the work and shares by construction: a
tenth template that uses existing names needs no code at all.

**It also makes renaming Loops optional.** `sessiondate` and `sessionDate` can be
two aliases onto one resolver, so the old and new generations of template both
work untouched. Renaming becomes housekeeping to do at leisure rather than a
nine-template editing pass blocking the first send.

---

## The three pieces

### 1. Discovery — what does this template want?

Loops offers **two** endpoints and both return `dataVariables` — *"Data variable
names used by the published email."*

| | use |
|---|---|
| `GET /v1/transactional-emails` | prime and refresh the whole cache in one call |
| `GET /v1/transactional-emails/{transactionalId}` | a single template, on a cache miss |

**Both, not one.** Bulk prime plus point lookup on miss is the ordinary shape for
a cache like this, and here the single endpoint earns its place specifically: the
likeliest cause of a name the cache has never seen is a template published
seconds ago, and fetching *that one template* is the precise answer. Refetching
all of them to learn one thing is the crude version.

Three things the implementation must get right:

- **Follow `nextCursor` on the list.** `perPage` maxes at 50 and the response is
  cursor-paginated. Nine templates fit one page today, so an implementation that
  reads page one and stops works perfectly now and silently drops templates the
  day you pass fifty. That is a bug that ships green.
- **Cache it.** Templates change rarely and messages send often; a fetch per send
  puts a Loops round trip on the send path for no new information.
- **Empty means refuse, never "needs nothing".** `dataVariables` is empty for
  *unpublished* templates. Treating that as "this template requires nothing"
  sends a blank email — the exact bug being fixed, wearing a convincing disguise.
  This is also why a cache miss must not be silently treated as "no variables".

**Discovery is a port**, with a Loops adapter and a static one, following `_rooms`
and `_calendar`. That buys three things: tests need no network, the static
adapter is a working fallback when Loops is slow, and a deployment can start
static and switch without a code change.

### 2. Resolution — what is the value?

A flat registry: **name → how to compute it** from the message context. The
context is the session, both parties, the recipient, and the settings — never a
database session, so resolvers stay pure and testable.

Discovery says *that* a template wants `sessionTopic`. It cannot say *how* to
produce one; that is what a resolver is, and it is the part that must live in
code.

### 3. The assertion — the whole point

**Every declared variable resolves, or the send fails naming the variable.**

Without this the feature is decoration. Loops accepts a missing key and renders
blank, so an unknown or unresolved name is invisible: a delivered email that
reads "Hi , your session on ". The assertion converts that into a `failed`
outbox row with a reason, which is what the outbox exists for.

Failing loudly is the deliberate trade. This design hands whoever edits Loops
control over what a message requires — that is the flexibility — and the cost is
that a template edit can break sends. Breaking visibly is the acceptable version
of that cost.

---

## The resolvers

### Core — available to every session message

| name | value |
|---|---|
| `recipientName` | the person this copy is addressed to |
| `mentorName` | always the mentor |
| `menteeName` | always the mentee |
| `sessionDate` | the date, in the **recipient's** timezone |
| `sessionTime` | the time, in the **recipient's** timezone |
| `sessionTimezone` | the zone those two are rendered in |
| `sessionTopic` | what the session is about |
| `sessionDetail` | what the mentee wants to discuss |
| `location` | the venue **label** — "Google Meet", "Daily". Never a URL |
| `sessionUrl` | the EduFurther **session page**. Never the meeting link |
| `dashboardUrl` | the recipient's dashboard |

**Resolved per recipient, not per message.** `sessionDate` differs between a
mentor in Lagos and a mentee in Toronto. The outbox already stores one row per
recipient, which is what makes this possible.

**`sessionTimezone` is not optional.** The standing rule here is that instants go
out as UTC and are never rendered server-side, because a cross-timezone session
has no single local time. Email forces the exception — it cannot render
client-side — so the zone must travel with the rendered time or the reader cannot
know which one they are reading.

### Message-specific

| name | for | value |
|---|---|---|
| `hours` | request, request reminder | hours left to answer, from `respond_by` |
| `intervalTime` | reminder | how far ahead this reminder is |
| `reasonTitle` | declined, withdrawn | the coded reason, in words |
| `reasonMessage` | declined, withdrawn, cancelled | what the person wrote |
| `cancelInitiator` | cancelled | which party called it off |
| `feedbackUrl` | feedback | where to leave it |

A resolver asked for something the message has no basis for — `hours` on a
cancellation — fails rather than returning empty. A template asking for it is a
template pointed at the wrong message, and that should be loud.

### Aliases

Registered so the current templates work **without being edited**:

| existing name | resolves as |
|---|---|
| `name` | `recipientName` |
| `attendee` | the other party's name |
| `sessiondate` | `sessionDate` |
| `sessiontime` | `sessionTime` |
| `topic` | `sessionTopic` |
| `topicDiscuss`, `discuss` | `sessionDetail` |
| `sessionlink`, `webUrl` | `sessionUrl` |
| `dashlink` | `dashboardUrl` |
| `cancelmessage` | `reasonMessage` |
| `cancelinitiator` | `cancelInitiator` |
| `intervaltime` | `intervalTime` |
| `feedbacklink` | `feedbackUrl` |

`attendee` is the one alias that is **not** a rename. It is recipient-relative —
"the other person" — where everything else is absolute. It resolves correctly,
but a template using it cannot be read without knowing who received the copy,
which is why the canonical set has no equivalent and why new templates should use
`mentorName` / `menteeName`.

---

## `sessionUrl` is the session page, never the meeting link

Written down because it is the thing somebody will later "fix" wrongly.

Meeting links are deliberately not shared ahead of time — partly so nobody joins
early, partly so a Join press is something the platform can record. An email
carrying the Daily or Meet URL hands it out days in advance and undoes both.

`sessionUrl` points at the EduFurther session page. The **Join button appears
there five minutes before the start** — `JOIN_OPENS` in `domain/attendance.py`,
matching the legacy application — and the API already publishes `join_opens_at`
so a client reveals it without computing anything.

---

## Configuration

`APP_BASE_URL` is a **new setting** and is not `PUBLIC_BASE_URL`. That one is this
service's own origin, used for Google's redirect and QStash's callback; an email
link built from it would send mentors to the API. `http://localhost:3000` for
now, changed by configuration rather than deploy.

The route shapes behind `sessionUrl` and `dashboardUrl` are undecided and live as
constants in one place, so settling them is a one-file change.

---

## Current templates

Ids are unchanged and `EMAIL_TEMPLATES` needs no edit for those already mapped.
With aliases registered, **none of these needs editing in Loops to work**.

| template | id | message |
|---|---|---|
| Session Confirmation | `clyamujcw008npblz54cv0oxw` | `session_booked`, and `request_accepted` |
| Session Request | `cmbxizcr3bcvrvs0idfrh81yo` | `session_requested` |
| Declined Request | `cmbxk5mzj1ldovu0iw608gzga` | `request_declined` |
| Withdrawn Request | `cmc4umpfe0a5y5e0in8n13flj` | `request_withdrawn` |
| Session Canceled | `clyvhvwru002hm392q9y8qeje` | `session_cancelled` |
| Session Request Reminder | `cmbxjqtne3nt1wu0i5sk5kr2h` | `mentor_response_reminder` |
| Session Reminder | `clyao8wx60024h2stw3o2ejh8` | `session_reminder` |
| Session Last Reminder | `clyaoph2m00xzs2yecm330s2u` | `session_last_reminder` |
| Session Feedback | `clyarw8rp01mezrlmid7xay2i` | **withdrawn — see below** |

`request_accepted` sharing Session Confirmation stops being a decision worth
agonising over: both resolve the same names, so sharing costs nothing and
splitting later is a config change.

**Two of those three gained producers and one lost its message.** The
pre-session reminders ship and fire; the feedback request was *withdrawn*
before it ever sent, because it conflated a platform survey to both parties
with a mentor review from the mentee.

So **Session Feedback is a template with no message**. Whether
`review_requested` reuses that id or gets its own is an operator decision in
Loops, not a code one — the mapping is configuration, which is the whole
point of `EMAIL_TEMPLATES`. What the copy has to change either way is the
audience: the withdrawn message addressed both parties, and a review request
addresses the mentee alone.

### Messages with no template

`request_expired`, `calendar_disconnected`, `mentor_approved`, `mentor_declined`,
**`review_requested`**, **`review_reminder`**.

**The last two are the operationally urgent ones**, and this list is where an
operator would look. `template_for()` raises `ConfigurationError` rather than
falling back — *"sending the wrong message is worse than sending none"* — so
until both are mapped in `EMAIL_TEMPLATES`, a settled session queues a review
request that fails at the drain rather than at the enqueue. Nothing is lost,
because the outbox retains the row; nothing is sent either.

The last three are not *session* messages: they resolve the name fields and
nothing else, because there is no session to describe. A resolver registry is
what makes that free rather than three more field lists.

---

## Open questions

- **`reasonTitle` needs a code-to-words mapping.** `SessionReasonCode` is a
  vocabulary policy reads; the human wording has to live somewhere, and Loops is
  the wrong place because a template cannot see the code.
- **How often to refresh the cache in the background.** The miss path is settled
  — fetch the single template — but a template whose variables *changed* rather
  than appeared produces no miss, so something has to re-read it eventually. A
  sweep beside the others in `settle_sessions` is the obvious home.
