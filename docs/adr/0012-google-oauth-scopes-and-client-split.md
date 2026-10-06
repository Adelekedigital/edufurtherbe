# 12. Google OAuth: non-sensitive scopes, and one Cloud project per purpose

Date: 2026-08-05

## Status

Proposed — **amended 2026-10-06**, while still `Proposed`, to widen the mentor's
consent to `openid email` beside `calendar.freebusy`. An amendment rather than a
supersession: this record's headline is that both integrations stay inside the
**non-sensitive scope tier**, and the amendment keeps that true rather than
breaking it. See *Amendment: naming the connected account* at the end of the
Decision.

## Context

Two accepted records depend on Google OAuth and neither names a scope. ADR 0004
decides that we own the OAuth client and that calendar is a write target and an
on-demand free/busy read. ADR 0009 makes Google the majority login path for the
1,200 migrated users. Both were written assuming sensitive-scope verification was
unavoidable, and both now carry a correction note saying it is not.

What neither record contains is the decision that follows: **which scopes, and how
the clients are arranged.** That is this record.

**The scope tiers, read from the Google Cloud console rather than recalled.** The
console groups them under "Your non-sensitive scopes" and "Your sensitive scopes —
approval required", which makes this a fact to be looked up rather than reasoned
about:

| Scope | Grants | Tier |
|---|---|---|
| `openid` | Associate the user with their Google identity | Non-sensitive |
| `.../auth/userinfo.email` | Primary account email address | Non-sensitive |
| `.../auth/userinfo.profile` | Name and public profile information | Non-sensitive |
| `.../auth/calendar.freebusy` | *"View your availability in your calendars"* | **Non-sensitive** |
| `.../auth/calendar.app.created` | *"Make secondary Google calendars, and see, create, change, and delete events on them"* | **Non-sensitive** |
| `.../auth/calendar.events` | *"View and edit events on all your calendars"* | Sensitive |
| `.../auth/calendar.events.owned` | Events on calendars the user owns | Sensitive |

**The surprising entry is `calendar.app.created`.** It is a full write capability —
create, change and delete — and it is non-sensitive, because the application can
only ever touch a calendar it created itself. Google draws the line at *whose data
you reach*, not at *what verbs you use*. That single fact is what makes the whole
of ADR 0004 achievable without a review.

**The consent screen belongs to the Cloud project, not the client.** Branding, the
scope list and verification status are project-level, so two OAuth clients inside
one project share all three. A sensitive scope added for calendar would therefore
attach to the sign-in client as well — and with it, any user cap. Sign-in is
1,200 users; calendar is 44 mentors. Coupling them puts the larger population
behind a limit that exists for the smaller one.

**Two behaviours this decision depended on were untested, and have now been
measured.** They were named here rather than assumed, because ADR 0009 made the
mistake of asserting a posture it had not measured and had to say so in its own
Confirmation section. Measured against real Google accounts on **2026-08-16** by
`scripts/calendar_spike.py`; the full results are in
`docs/calendar-spike-guide.md`.

1. **An event on an app-created secondary calendar does send normal attendee
   invitations.** Guests receive an ordinary Google Calendar invitation having
   authorised nothing, which is what ADR 0004's mechanism for reaching 1,200
   mentees requires.
2. **That calendar's busy intervals do appear in the mentor's own free/busy** —
   but **not immediately**. A query issued seconds after the write returns an
   empty `busy`; the same window minutes later returns the interval. The first
   two runs read the lag as a scoping rule and recorded the wrong answer.

Three things the spike measured that this record did not think to ask, and one of
them changes the decision below:

3. **The invitation's sender is the account whose token made the call** — not the
   calendar. The API's `organizer` field names the app-created calendar and is
   *not* what a recipient sees; the `creator` is. So writing from a mentor's
   account puts **the mentor's personal email address** on every invitation.
4. **Invited guests join the Meet room with the creating account absent**, and
   without knocking. The bypass is keyed to the address on the invitation, so a
   guest signed into a different Google account is treated as a stranger.
5. **A Google Meet link can be minted on these scopes**, on the same event the
   invitation carries. It requires `conferenceDataVersion=1` on the insert;
   without it the API accepts the write and silently drops the conference data,
   which is indistinguishable from a refusal.

**Nothing has consented yet.** The 1,200 have not migrated and no mentor has
reconnected since ADR 0004. Whatever arrangement exists when they do is the one
they consent to, and changing it afterwards means asking all of them again.

That window is exactly why the change in point 3 below is free. Moving the
calendar from the mentor's account to EduFurther's narrows what every mentor is
asked to grant; had a single mentor already consented to the wider pair, this
would have been a re-consent exercise across all of them instead of an edit.

## Decision

**Both Google integrations stay inside the non-sensitive scope tier, and sign-in
and calendar live in separate Cloud projects.**

1. **Calendar uses exactly two scopes, granted by two different parties.**
   `.../auth/calendar.freebusy` is granted **by each mentor**, to read their
   availability. `.../auth/calendar.app.created` is granted **once, by
   EduFurther's own Google account**, to write events. Nothing else.
   `calendar.events.public.readonly` is dropped because nothing needs it.
   `calendar.events.freebusy` is dropped as **apparently** redundant against
   `calendar.freebusy` — apparently, because the overlap has not been verified and
   the open question below asks whether `calendar.freebusy` reaches a mentor's
   institutional calendar. If it does not, the two are not redundant at all, one
   is simply broader, and this choice is the one to revisit first.

   The split means **a mentor's consent screen asks for one calendar thing**:
   *"View your availability in your calendars."* It is the narrowest calendar
   permission Google offers, and it is the whole of the calendar ask.

   *Amended 2026-10-06:* the screen also shows *"See your primary Google Account
   email address"*, from the `openid email` added below. The calendar sentence
   above is unchanged — the amendment adds nothing sensitive and nothing that
   touches a calendar.
2. **Sign-in uses `openid`, `.../auth/userinfo.email` and
   `.../auth/userinfo.profile`**, and never a calendar scope.
3. **Session events live in a secondary calendar the application creates in
   EduFurther's own Google account**, with the mentor and the mentee both as
   attendees. Not in the mentor's account — that was this record's original
   choice and the spike falsified its premise.

   **The sender is the account that writes.** A mentor-owned calendar puts the
   mentor's personal email address on every invitation a mentee receives, which
   is not a setting that can be changed: it follows from whose token makes the
   call. A platform-owned calendar makes EduFurther the sender and keeps both
   participants' mailboxes out of it.

   Three properties follow. **The second is measured; the first and third are
   not**, and saying so matters in a record that opens by faulting ADR 0009 for
   asserting a posture it had not tested:

   - **The session should land on the mentor's primary calendar** when they
     accept — their action, not our write — which would be the placement ADR 0004
     files under capabilities needing a sensitive scope, obtained without one.
     **Inferred, not measured.** Every attendee in the spike stayed at
     `needsAction`; nobody accepted an invitation, so this is ordinary Google
     Calendar behaviour rather than something observed here. It is the cheapest
     remaining test and is listed in Confirmation.
   - **No host need be present.** Both invited guests joined the Meet room, each
     signed in as their invited account, **with the creating account absent from
     the call** — so the platform account never appears in a session. The bypass
     is keyed to the address on the invitation rather than to holding the link,
     so a guest signed into some other Google account is treated as a stranger
     and must knock.
   - **The mentor's calendar connection becomes an enhancement, not a
     prerequisite.** Booking works whether or not a mentor ever connects Google,
     because the event is ours to create. What the grant buys is conflict
     detection against their real calendar. A mentor who never connects can still
     be booked; they can simply be double-booked. This is a materially smaller
     onboarding requirement than a mentor-owned calendar allows, and it means
     booking can ship before calendar connection exists. **A consequence of where
     the event lives rather than a measurement** — it needs no test, but it is
     reasoning, not observation.

   The cost is that **the mentor is a guest at their own session and can decline
   it.** See Open Questions.
4. **Two Cloud projects.** Sign-in is consumed by Supabase Auth (ADR 0009);
   calendar is consumed by **our own adapter, calling the Google Calendar API
   directly** — see point 6. Separate projects, separate consent screens,
   consistent branding across both so a mentor sees two prompts that obviously
   come from the same product.
5. **The split happens before the migration**, not after. It is nearly free now
   and costs a re-consent from every user later.
6. **Calendar talks to Google directly, behind a `CalendarPort`.** No integration
   platform. This **supersedes ADR 0004's** *"configure it in Composio as a
   custom auth config"*; the rest of that record — owning the client, calendar as
   a write target and an on-demand free/busy read, never polled and never
   mirrored — is untouched and still governs.

   The reason is in the two records themselves rather than in a new preference.
   ADR 0004 was written believing calendar needed the **sensitive** tier, so
   verification was unavoidable and managed auth looked like help. The scope
   table above shows it does not: `calendar.freebusy` and `calendar.app.created`
   are both non-sensitive, so there is **no review to survive**. And ADR 0004
   already established that verification attaches to the OAuth client rather than
   the middleware — *"whether orchestrated by Composio, by Nango, or by our own
   code, the client is ours to register and ours to get verified. No integration
   platform removes this step, so it cannot be a reason to choose between them."*

   Take away the review and the managed auth and what remains is a per-tool-call
   bill for HTTP requests we can make ourselves, plus a vendor between us and a
   consent screen we own — which is exactly the failure ADR 0004 opens with,
   where every connected account broke because the shared OAuth application's
   publishing status was not ours to see.

   The port is the hedge. `domain/ports.py` owns the interface, `infra/` holds
   the Google adapter, and a later move to Nango or a second provider changes the
   adapter rather than the calling code (see the **Port** entry in the domain
   vocabulary).
6. **Staying inside the non-sensitive tier is a boundary we are choosing**, not an
   accident of what was available. Crossing it is a decision with its own record —
   see the closing note below.

**The pairing is narrower than ADR 0004 contemplated, and deliberately so.**
`calendar.freebusy` reveals *when* a mentor is busy and never *what* they are
doing. `calendar.app.created` means the application cannot read or alter anything
it did not write. ADR 0004 observed that free/busy gives the right privacy
boundary "as a property of the chosen read primitive rather than a policy we have
to enforce separately"; this record extends the same property to the write side.

Narrower still, now the write lives in EduFurther's account: the application does
not merely refrain from touching a mentor's other events, it **holds no write
capability on their account at all.** The only thing a mentor grants is a read of
when they are busy.

### Amendment: naming the connected account (2026-10-06)

**The mentor's consent now asks for `openid email` as well**, and the account's
email address is stored on the connection and returned by
`GET /api/v1/me/calendar` as `account_email`.

**Why this record's premise did not survive.** The original reasoning was that
being able to name the account was worth less than the narrowness of the ask.
That weighed the right things and got one fact wrong: `openid` and `email` are
**non-sensitive**, and Google sets an app's verification requirement by the
**most sensitive** scope it requests. So the increment cannot raise the tier,
whatever tier `calendar.freebusy` turns out to occupy — if that scope is
sensitive the app already needs that verification and two non-sensitive
additions change nothing; if it is not, the set stays non-sensitive. There is no
third branch, because a non-sensitive scope cannot be the most sensitive one in
a set. The cost is **one sentence on a consent screen**, not an operational
burden, and this record's headline — both integrations inside the non-sensitive
tier — is untouched.

**`calendar.freebusy` is non-sensitive, and this is now measured rather than
assumed.** Google publishes no tier for it — the Calendar scopes table states
none, and the Cloud console assigns it at configuration time, so neither this
record nor the frontend could establish it from the documentation. On 2026-10-06
the owner configured the mentor-facing client and the console listed it under the
**non-sensitive** scopes, beside `openid`, `userinfo.email`, `userinfo.profile`
and `calendar.events.freebusy`:

| Scope | Console's description |
|---|---|
| `.../auth/calendar.events.freebusy` | See the availability on Google calendars you have access to |
| `.../auth/calendar.freebusy` | View your availability in your calendars |
| `openid` | Associate you with your personal info on Google |
| `.../auth/userinfo.email` | See your primary Google Account email address |
| `.../auth/userinfo.profile` | See your personal info, including any personal info you've made publicly available |

So this record's central claim holds as fact and not as intent: **the whole
mentor grant sits in the non-sensitive tier, and the app needs no sensitive-scope
verification at all.** The branch reasoning above was sound but is no longer
needed — the favourable branch is the real one.

Two things follow. `calendar.events.freebusy`, dropped above as *apparently*
redundant, is also non-sensitive, so revisiting that choice costs no verification
either — relevant to this record's remaining open question about institutional
calendars, which this does **not** answer. And the amendment's cost is confirmed
as exactly one consent-screen line, since `userinfo.email` is the scope whose
description is that line.

**What it buys.** `calendar.freebusy` cannot name the account it was granted by:
`calendarList.list` is outside it, so there is no second route. Without a name a
mentor who authorised the **wrong Google account** has nothing on the page
telling them — they see "Connected" and a calendar that subtracts the wrong
person's busy hours, or nobody's. The escape hatch for that problem is useless
to somebody who cannot see they have it.

**Why now and not later, which is the whole of the timing.** A scope change does
not retro-fit an existing grant. A mentor who consented to less carries no email
in their token and gets one only by consenting again — and re-consenting is the
step most likely to make somebody abandon an integration. When this was decided,
**no environment had a mentor-facing client configured and no mentor had
connected anything** (#366 was still open), so the change cost nothing at all.
The same change a week later costs every connected mentor a re-consent. The
window was open exactly once.

**What is stored, and what is not.** The email goes in a new nullable
`calendar_connections.external_account_email`. It does **not** go in
`external_account_id`, which this record's phase notes reserve for the stable
`sub` claim: an email can change and a `sub` cannot, so one is a label and the
other an identifier. `external_account_id` stays null, because nothing reads an
identifier yet and a column filled for no reader invites the next person to
assume it is load-bearing.

**Null means *not known*, never *none*.** There is nothing to backfill from —
the address lives in a token the mentor no longer holds — so a grant predating
this amendment reads null for ever, and a client degrades to "Connected".

**The email is read from Google's userinfo endpoint, not the `id_token`.** The
grant now returns an ID token, and reading the claim out of it would mean either
verifying its signature — a JWT library, Google's keys and their rotation, on
the calendar path — or trusting it unverified. Google's documentation says to
verify an ID token's signature and recommends a library in production; the
exemption often assumed for a token fetched directly from the token endpoint is
not something those docs state, so it is not relied on here. One authenticated
call with a token Google issued seconds earlier needs none of it.

**A failed lookup does not refuse the connection.** By the time the email is
read, the mentor has completed a consent and Google has issued a refresh token:
the connection is made. So the lookup answers null on any failure, and the grant
is stored unnamed. The alternative is a `502` for a consent that succeeded,
telling a mentor nothing worked while their calendar is in fact connected.

### Rejected alternatives

**`calendar.events`, with sensitive-scope verification.** The obvious reading of
ADR 0004, and what that record assumed. It buys the ability to write into the
mentor's primary calendar, which is where most people expect appointments to
appear. Rejected because the cost is an app review, a 100-user cap and an
unverified-app warning until it clears — and it buys write access to *every* event
on *every* calendar the mentor has, to solve a problem a dedicated calendar solves
with no review at all. The strongest argument for it is placement, and placement
is a preference rather than a capability.

**And the placement argument has since evaporated entirely.** Under the
platform-owned arrangement in point 3, the session reaches the mentor's primary
calendar by invitation — so the one thing this scope was worth buying is
obtained without buying it. What remains on its side of the ledger is nothing.

**Two OAuth clients inside one Cloud project.** Simpler: one project, one
brand-verification exercise, one thing to administer. Rejected because it does not
work — the consent screen and its scope list are project-level, so the clients
would share a verification status and a user cap. It has the appearance of
separation and none of the substance, which is worse than not separating at all.

**Email an ICS invitation and write no calendar at all.** Removes the write scope
entirely and would have removed a review even under the old assumption. Rejected
because an emailed attachment is materially worse than a real calendar event — no
update propagation on reschedule, no cancellation, no RSVP — and because
`calendar.app.created` costs nothing that this saves. It is recorded because it
was the fallback while sensitive scopes still looked unavoidable, and someone
should know it was considered and why it stopped being necessary.

The platform-owned arrangement makes this rejection stronger than it was written.
Update propagation, cancellation and RSVP are properties of an event **we own
outright** — not one living in a mentor's account, subject to their edits and
lost with their grant. The reasoning is unchanged; the gap it describes is wider.

**Requesting `calendar.events.owned` instead of `calendar.events`.** Narrower, and
still sensitive. It carries the review without avoiding it, so it is the worst of
both — mentioned only because "pick the narrower sensitive scope" is the reflex
this record replaces with "check whether a non-sensitive one does the job".

Mirroring or polling calendar state is **not** reconsidered here. ADR 0004
rejected it on cost and freshness, and nothing in this record touches that
reasoning.

## Consequences

**Session events reach the mentor's primary calendar after all**, by invitation
rather than by write. This paragraph previously recorded the opposite — that
events sit in a separate calendar, that it is "the one user-visible cost of this
decision", and that it "is not reversible without moving to a sensitive scope".
All three were true of the mentor-owned arrangement and none survives the
platform-owned one: an accepted invitation is the mentor's own action, so the
placement lands on the right side of the tier boundary without crossing it.

**Neither participant's email address is the sender.** Invitations come from
EduFurther's own account. Under the original arrangement they would have carried
the mentor's personal address, which nothing in this record had noticed and no
configuration could have changed.

**The mentor is an attendee at their own session.** They can decline, and their
acceptance is what places the session on their calendar. This is the cost paid
for the two properties above, and the booking flow needs an answer for a declined
mentor — see Open Questions.

**A mentor who never connects Google can still be booked.** The connection buys
conflict detection, not the ability to hold a session. Calendar connection is
therefore not on the critical path for booking, which is the opposite of what a
mentor-owned calendar would have required.

**The application can never read or alter anything it did not write.** Not by
policy, by construction. A bug, a compromised token or a careless later feature
cannot reach a mentor's other events, because the grant does not extend there.
This is a materially better security posture than ADR 0004 planned for, and it
makes the privacy policy shorter and truthful in a way that is easy to verify:
*we can see when you are busy, and we manage the sessions calendar we created.*

**No app review, no user cap, no unverified-app warning** — for either integration.
The external dependency both ADR 0004 and ADR 0009 placed on the critical path is
removed rather than reduced.

**Mentors see two consent screens** — one at sign-in, one when they connect a
calendar. Mentees see one. This is the visible cost of the project split and is
worth the isolation it buys.

The second is now **optional and narrower than it was**. Optional, because
booking does not depend on it; narrower, because it asks only *"View your
availability in your calendars"* rather than that plus a write capability. A
mentor who refuses it is still bookable, which is the difference between a
consent screen that gates onboarding and one that improves it.

**ADR 0004's Decision is superseded in two places.** It says "We submit it for
sensitive-scope verification" — replaced by the scope table above — and it says
"We stay on Composio", with calendar configured there as a custom auth config,
which point 6 replaces. Its status should gain `points superseded by ADR 0012`
**on acceptance**, following the sequencing ADRs 0008 and 0009 used — a status
naming a supersession by a record still `Proposed` would assert a decision nobody
has taken. Everything else in ADR 0004 stands: owning the client, the
write-and-read-on-demand shape, availability remaining mentor-declared, and only
mentors connecting.

**We are leaving Composio for a reason ADR 0004 did not list, and that is worth
saying plainly.** That record permits departure "if and only if" one of three
things becomes true — the action catalogue falls short, token custody becomes a
requirement, or we adopt polling. None has. What changed is upstream of all
three: the verification this record retires was a large part of what the platform
was being paid to absorb, and once there is no review to survive, an on-demand
free/busy call and a secondary-calendar write are two ordinary HTTPS requests.
The exit criteria were written to catch Composio becoming *insufficient*; this is
Composio becoming *unnecessary*, which is a better problem and needs recording as
a different one rather than filed under the nearest existing clause.

The token-custody point is worth noting as a benefit rather than a motive. ADR
0004 records that Composio holds our users' OAuth tokens and we cannot inspect
that, and names it the strongest argument for leaving. Going direct resolves it
as a side effect — but it did not drive the decision, and dressing it up as
condition 2 would rewrite history to make the exit look pre-authorised.

**Settled decision #15 is unchanged.** Calendar is still "a write target and an
on-demand free/busy read"; this record decides *where* the write lands, which is
detail the ADR carries and the row does not need.

### For the phase that builds `calendar_connections`

The table is deferred — its DDL is in the package's `03_availability.sql` and
nothing in this repository creates it (settled decision #21: it ships with the
phase that first needs it). Three things about that DDL are **wrong for this
decision**, and none is visible to any gate. The package is canonical and is not
edited here (ADR 0007), so they are recorded against it rather than in it.

- **Do not create `calendar_provider` or `connection_status` as PostgreSQL
  enums.** The package declares both in `00_foundation.sql`. Neither exists in
  our schema, and neither appears in `docs/handoff-enum-to-text-check.md` —
  because that document inventories what *is* there. Building them verbatim adds
  two new types to precisely the debt that handoff exists to retire. Settled
  decision **#100**: a closed set is `text` + `CHECK` + a `StrEnum` at the
  Pydantic boundary.
- **Omit `composio_auth_id`.** Point 6 above removes Composio, and the legacy
  values are already known-dead from the managed-auth outage. It would be a dead
  column on a table that has never existed.
- **A row records a mentor's `calendar.freebusy` grant and nothing else.** The
  platform account's credential is configuration — it flows through
  `core/config.py` as a `SecretStr` (non-negotiable 6) — and is not a user row.
  `user_id` therefore always identifies a mentor, never EduFurther.

**A later capability may still need a sensitive scope**, and this record does not
foreclose it. Writing into a mentor's primary calendar, or reading event details
to show a mentor what they are double-booked against, would both require crossing
the tier. That is a decision to take when the capability is actually wanted, with
its own record and its own accounting of the review cost — not a door closed here.

### Confirmation

- **Done, 2026-10-06, and it was never checkable from here:** no sensitive scope
  appears in the mentor-facing Cloud project. The owner configured the client and
  reported its scope list; every entry sat under **non-sensitive**, including
  `calendar.freebusy` itself, whose tier Google publishes nowhere. The table is in
  the amendment above. This remains a review step against this record rather than
  a test — the scope list lives in the console, and nothing in this repository can
  see it, which is why it had stood unconfirmed since August.
- **Measured, and it was the largest gap:** an event on an app-created secondary
  calendar *does* send attendee invitations, so ADR 0004's mechanism for reaching
  1,200 mentees without an OAuth flow holds. This record was written on
  documentation rather than on a working flow — the same caveat ADR 0007 made
  about adopting a schema on reading — and `scripts/calendar_spike.py` is what
  replaced the reading with a measurement.
- **Measured:** the secondary calendar's busy intervals *do* appear in the
  mentor's own free/busy, after an indexing delay. Re-run the spike to confirm
  rather than trusting a single query, because **the first two runs recorded the
  wrong answer** — they queried seconds after the write and read the lag as a
  scoping rule.
- **Not measured, and cheap to measure:** nobody has accepted one of these
  invitations, so the claim that an accepted session lands on the mentor's
  primary calendar is ordinary Google behaviour rather than something observed.
  It is what makes the placement argument work, so it should be confirmed before
  calendar code ships — accept a spike invitation from a second account and look
  at that account's calendar.
- **Not mechanical, and new:** nothing detects the OAuth app's publishing status
  reverting to Testing, which silently caps the platform account's refresh token
  at seven days. The integration would fail weekly with no signal in this
  repository.
- **Not mechanical:** nothing detects a scope being added to either project later.
  A sensitive scope added in the console is invisible to CI, to this repository,
  and to everyone except whoever next opens the consent screen.

### Open questions

- ~~**Do attendee invitations send from an app-created calendar?**~~ **Answered:
  yes.** Measured 2026-08-16.
- ~~**Does the secondary calendar count toward the mentor's own free/busy?**~~
  **Answered: yes, after an indexing delay.**
- **What happens when a mentor declines their own session?** They are an attendee
  now. Declining does not cancel the booking, and the mentee holds an invitation
  to a session the mentor has refused. Needs an answer before booking ships.
- **Is the platform's refresh token durable in practice?** Google issues refresh
  tokens expiring in **seven days** while an OAuth app's publishing status is
  *Testing*; published, they last until revoked or six months unused. Publishing
  requires no verification here, because both scopes are non-sensitive — but
  nothing in this repository can observe the status.
- **How is a revoked mentor grant handled?** Booking still works without it, so
  the failure degrades to no conflict checking rather than an outage. Whether
  that may happen silently, or must be surfaced to the mentor, is undecided.
- **One platform calendar for every session, or one per mentor?** The spike
  created one per run and never had to choose. Nothing depends on it yet; it
  determines whether anything must be stored per mentor beyond their grant.
- **Does `calendar.freebusy` reach a mentor's institutional calendar** — the shared
  one their teaching sits on, which they have access to but do not own? If not,
  availability has a gap exactly where an academic's real commitments live, and
  `calendar.events.freebusy` may be the better of the two after all.
- ~~**Does Composio support a custom auth config pointing at a second Cloud
  project** cleanly?~~ **Moot — see point 6.** Calendar calls Google directly, so
  there is no integration to price. This question is what prompted the
  re-examination: an open cost against a vendor whose only remaining contribution
  was billing for requests we can make ourselves.
