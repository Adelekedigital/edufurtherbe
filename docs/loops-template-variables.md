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
| `suggestedDate` | time suggested, its reminder | the offered time's date, in the mentee's zone |
| `suggestedTime` | time suggested, its reminder | the offered time, `HH:MM` in the mentee's zone |
| `holdUntilTime` | time suggested, its reminder | when the hold lapses, `HH:MM` in the mentee's zone |
| `suggestedAfter` | time suggested | `declined` or `cancelled` — what happened to the original |

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
| `sessTopic` | `sessionTopic` |
| `fName` | the recipient's first name |
| `bookLink` | the Explore page |
| `reviewBy` | `menteeName` (the review's author) |
| `reviewFor` | `mentorName` (who the review is about) |
| `reviewLink` | `sessionUrl` (where the review lives) |
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

## Template registry

**The one reference for which Loops template each email uses.** Every
`Notification` member has exactly one row, and `tests/unit/test_template_registry.py`
fails when a member is missing, so code and this table cannot drift. Ids are the
Railway `development` mapping as of 2026-10-03; ids are not secret. Variables are
what the live template declared when read from Loops on 2026-10-03; the backend
reads them again from Loops at send time, so this column is a record, not a
contract.

| member | Loops template | id | receives it | variables |
|---|---|---|---|---|
| `session_booked` | Session Confirmation | `clyamujcw008npblz54cv0oxw` | mentor | `attendee`, `location`, `name`, `sessiondate`, `sessionlink`, `sessiontime`, `topic`, `topicDiscuss` |
| `session_requested` | Session Request | `cmbxizcr3bcvrvs0idfrh81yo` | mentor | `discuss`, `hours`, `menteeName`, `mentorName`, `sessionDate`, `sessionTime`, `sessionTopic`, `webUrl` |
| `request_accepted` | Session Confirmation (shared) | `clyamujcw008npblz54cv0oxw` | mentee | as `session_booked` |
| `request_declined` | Declined Request | `cmbxk5mzj1ldovu0iw608gzga` | mentee | `menteeName`, `mentorName`, `reasonMessage`, `reasonTitle`, `sessionDate`, `webUrl` |
| `request_withdrawn` | Withdrawn Request | `cmc4umpfe0a5y5e0in8n13flj` | mentor | as `request_declined` |
| `session_cancelled` | Session Canceled | `clyvhvwru002hm392q9y8qeje` | the other party | `cancelinitiator`, `cancelmessage`, `dashlink`, `name`, `sessiondate` |
| `request_expired` | — | **NEEDED** | both | — |
| `mentor_approved` | — | **NEEDED** | the mentor | — |
| `calendar_disconnected` | — | **NEEDED** | the mentor whose calendar it is | — |
| `session_reminder` | Session Reminder | `clyao8wx60024h2stw3o2ejh8` | both | `attendee`, `intervaltime`, `location`, `name`, `sessiondate`, `sessionlink`, `sessiontime`, `topic`, `topicDiscuss` |
| `session_last_reminder` | Session Last Reminder | `clyaoph2m00xzs2yecm330s2u` | both | as `session_booked` |
| `review_requested` | sessionReviewRequest | `cmf1qlt600bjiut0iu5i7yycj` | mentee | `reviewBy`, `reviewFor`, `reviewLink`, `sessTopic` |
| `review_received` | reviewUpdateToMentors | `cmf1rd22ty7dcxz0iffc6jymd` | mentor | `reviewBy`, `reviewFor`, `reviewLink` |
| `credits_granted` | Credit: New users update | `cmbn678u30iwz4x0ixz20yfod` | the user | `bookLink`, `fName` |
| `credits_renewed` | Credit renewal | `cmbk0mv700e4yzn0i6ho1h75s` | the user | `bookLink`, `fName` |
| `credits_expiring` | Unused credit | `cmbk13nzg0iv4xw0i90uq1j5s` | the user | `bookLink`, `creditCount`, `fName` |
| `mentor_application_received` | — | **NEEDED** | admins who can decide it, never the applicant | — |
| `mentor_declined` | — | **NEEDED** | the applicant | — |
| `mentor_response_reminder` | Session Request Reminder | `cmbxjqtne3nt1wu0i5sk5kr2h` | mentor | `discuss`, `hours`, `location`, `menteeName`, `mentorName`, `sessionDate`, `sessionTime`, `sessionTopic`, `webUrl` |
| `mentor_return_reminder` | Return reminder | `cm4w04jbv00r82fxqlo8c6vyh` | the paused mentor | `mentorName` |

**Coming:** #339 ("Suggest a new time", in progress on another branch) adds two
members, the suggestion email and the hold reminder. That branch adds their rows.

**A member marked NEEDED fails at the drain** (`template_for()` raises), and the
outbox keeps the row, so nothing is lost and nothing is sent until an id is set.

### Paste-ready

Every mapped id, as one value. `EMAIL_TEMPLATES` **replaces the whole map**:
there is no merge, so adding a message means re-stating them all.

```
EMAIL_TEMPLATES={"session_booked":"clyamujcw008npblz54cv0oxw","request_accepted":"clyamujcw008npblz54cv0oxw","session_requested":"cmbxizcr3bcvrvs0idfrh81yo","request_declined":"cmbxk5mzj1ldovu0iw608gzga","request_withdrawn":"cmc4umpfe0a5y5e0in8n13flj","session_cancelled":"clyvhvwru002hm392q9y8qeje","mentor_response_reminder":"cmbxjqtne3nt1wu0i5sk5kr2h","session_reminder":"clyao8wx60024h2stw3o2ejh8","session_last_reminder":"clyaoph2m00xzs2yecm330s2u","review_requested":"cmf1qlt600bjiut0iu5i7yycj","review_received":"cmf1rd22ty7dcxz0iffc6jymd","credits_granted":"cmbn678u30iwz4x0ixz20yfod","credits_renewed":"cmbk0mv700e4yzn0i6ho1h75s","credits_expiring":"cmbk13nzg0iv4xw0i90uq1j5s","mentor_return_reminder":"cm4w04jbv00r82fxqlo8c6vyh"}
```

### Values that may be absent

Since 2026-10-03, a value a real event often lacks has one defined fallback
instead of failing the send (the dev outbox showed `session_requested` and
`request_declined` failing on them). An unknown variable **name** still fails.

| variable | value | when absent |
|---|---|---|
| `sessionTopic` (`topic`, `sessTopic`) | the topic written at booking | the offering's name, else "Mentorship session" |
| `sessionDetail` (`discuss`, `topicDiscuss`) | the booking message | empty |
| `location` | the venue label | "Online" |
| `reasonTitle` | the reason code in words (`REASON_TITLES`) | empty when a reason was written without a code; "No reason given" when neither |
| `reasonMessage` (`cancelmessage`) | what the person wrote | empty |
| `cancelInitiator` (`cancelinitiator`) | the name of whoever cancelled, read at send time | (always set by the cancel) |
| `fName` | the recipient's first name | their full name |
| `reviewBy` / `reviewFor` | the mentee (author) / the mentor (subject) | — |
| `reviewLink` | the session page, where the review lives | — |
| `bookLink` | the Explore page | — |
| `creditCount` | credits expiring on that date, carried on the row | required |

### A suggested time (#339)

`session_time_suggested` and `session_suggestion_reminder` (2026-10-03, decision
230) need their own templates and `EMAIL_TEMPLATES` entries on dev before a
suggestion can be delivered; until then the rows wait in the outbox.

- **`session_time_suggested`** replaces the decline or cancellation email when
  the mentor offered another time — the mentee gets **one** message saying the
  session is off and what is offered instead. May use `recipientName`,
  `mentorName`, `sessionDate`/`sessionTime` (the original), `suggestedAfter`,
  `reasonMessage` (empty when the mentor wrote nothing — word it so),
  `suggestedDate`, `suggestedTime`, `holdUntilTime` and `sessionUrl` (the
  original session's page, where the offer is shown and booked).
- **`session_suggestion_reminder`** goes thirty minutes before the two-hour
  hold lapses, only while the offer is still unbooked. Same names, without
  `suggestedAfter` and `reasonMessage`.

### Notes per message

`mentor_return_reminder` (2026-10-01) is **one template for two cases**, sent to
a self-paused mentor at 08:00 in their own zone. **The template must word both.**

- **A pause with a return date:** sent a week before the date, three days
  before, and on the day. `daysUntilReturn` is `7`, `3` or `0` and `returnOn` is
  the date in words; `daysPaused` is empty.
- **A pause with no date ("Not sure yet"):** sent 30 and 59 days after the pause
  began. `daysPaused` is `30` or `59`; `daysUntilReturn` and `returnOn` are
  empty.

A step already past when the pause is set is skipped, a run after a gap sends
only the latest step due, and a step not sent on its own day is never sent, so
a short pause may get only the last. The
template may use:

- `recipientName`
- `daysUntilReturn`: `7`, `3` or `0` (`0` is "today"), or empty with no date
- `returnOn`: the return date in words (e.g. `Saturday 03 October 2026`), or empty
- `daysPaused`: `30` or `59` with no date, else empty
- `calendarUrl`: where they switch back on

**The last two are the operationally urgent ones**, and this list is where an
operator would look.

`review_requested` maps to `sessionReviewRequest` and covers the nudge a day
later as well as the first ask — the repeat carries `interval`, whose absence
marks the original, so one template renders both.

`review_received` maps to `reviewUpdateToMentors` and is the only review
message addressed to a mentor. It should **link rather than quote**: the
author has ten minutes to correct a typo, which `sessionUrl` already settles —
the link is where the content lives. `template_for()` raises `ConfigurationError` rather than
falling back — *"sending the wrong message is worse than sending none"* — so
until both are mapped in `EMAIL_TEMPLATES`, a settled session queues a review
request that fails at the drain rather than at the enqueue. Nothing is lost,
because the outbox retains the row; nothing is sent either.

The last three are not *session* messages: they resolve the name fields and
nothing else, because there is no session to describe. A resolver registry is
what makes that free rather than three more field lists.

---

## Open questions

- ~~**`reasonTitle` needs a code-to-words mapping.**~~ **Settled 2026-10-03:**
  `REASON_TITLES` in `domain/messages.py`, pinned to cover every code.
- **How often to refresh the cache in the background.** The miss path is settled
  — fetch the single template — but a template whose variables *changed* rather
  than appeared produces no miss, so something has to re-read it eventually. A
  sweep beside the others in `settle_sessions` is the obvious home.


## The credit messages

Three templates, given by the owner on 2026-08-26. **All three now have
producers**, so all three are members and all three are mapped — settled decision
#21 is satisfied in the direction it cares about: no member without something
that writes it.

**`Credit: New users update` → `credits_granted`.** Fires on **profile
completion**, not on signup, and that is a correction rather than a preference.
The starter credit is *granted* on completion — deliberately, because signing up
is free and finishing a profile is work, which is the whole anti-farming
property. Sent at signup the message would tell somebody they hold a credit
before the lot exists.

The copy therefore has to say the credit *has arrived*, not that it is waiting.
If the intent is instead to prompt a new signup to finish their profile, that is
a second, different message and it has no producer.

**`Credit renewal` → `credits_renewed`. Built.** Enqueued by
`grant_monthly_credits` for the users it actually paid, derived from the same
`RETURNING` as the ledger rows — so the period guard refusing a second run in one
month refuses the second email with it, and a run that grants nothing tells
nobody.

**`Unused credit` → `credits_expiring`. Built**, as a daily sweep at fourteen and
seven days before an expiry.

The hazard this section predicted was real and its prescription was not.
*"The query must filter `expires_at IS NOT NULL` or it will warn people about a
credit that is not going anywhere"* — the outcome is right and the clause is
unconditionally redundant. The sweep matches a **bounded window** on both sides
of the offset, and a lot with no expiry has no date to fall inside one. The
explicit clause was written, then removed after two attempted rewrites failed to
make it matter: widening the range with `coalesce(expires_at, 'infinity')` still
excludes the starter, because a sentinel far future is outside any window an
offset produces.

Worth recording because the reasoning generalises: `NULL` is excluded from a
one-sided predicate *by comparison semantics*, which a rewrite can undo, and from
a two-sided one *structurally*, which it cannot.

**Its `kind` carries the period** — `c14:2026-10` rather than `c14`.
`uq_outbox_events_reminder` is unique on `(entity_id, event_type, kind,
recipient)` and `entity_id` here is the user, so a bare kind would be unique
across their lifetime and they would be nudged once, ever.
