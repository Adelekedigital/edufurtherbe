# 12. Google OAuth: non-sensitive scopes, and one Cloud project per purpose

Date: 2026-08-05

## Status

Proposed

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

**Two behaviours this decision depends on have not been tested.** They are named
in Confirmation and Open Questions rather than assumed, because ADR 0009 already
made the mistake of asserting a posture it had not measured and had to say so in
its own Confirmation section:

1. Whether an event on an app-created secondary calendar sends normal attendee
   invitations. ADR 0004 requires mentees to receive an invitation while
   completing no OAuth flow, so this is load-bearing.
2. Whether that calendar's busy intervals appear in the mentor's own free/busy —
   without which two EduFurther sessions would not block against each other.

**Nothing has consented yet.** The 1,200 have not migrated and no mentor has
reconnected since ADR 0004. Whatever arrangement exists when they do is the one
they consent to, and changing it afterwards means asking all of them again.

## Decision

**Both Google integrations stay inside the non-sensitive scope tier, and sign-in
and calendar live in separate Cloud projects.**

1. **Calendar uses exactly two scopes**: `.../auth/calendar.freebusy` to read
   availability, and `.../auth/calendar.app.created` to write events. Nothing
   else. `calendar.events.public.readonly` is dropped because nothing needs it.
   `calendar.events.freebusy` is dropped as **apparently** redundant against
   `calendar.freebusy` — apparently, because the overlap has not been verified and
   the open question below asks whether `calendar.freebusy` reaches a mentor's
   institutional calendar. If it does not, the two are not redundant at all, one
   is simply broader, and this choice is the one to revisit first.
2. **Sign-in uses `openid`, `.../auth/userinfo.email` and
   `.../auth/userinfo.profile`**, and never a calendar scope.
3. **Session events live in a secondary calendar the application creates** in the
   mentor's Google account. This is the mechanism `calendar.app.created` provides
   and the reason it is non-sensitive.
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

### Rejected alternatives

**`calendar.events`, with sensitive-scope verification.** The obvious reading of
ADR 0004, and what that record assumed. It buys the ability to write into the
mentor's primary calendar, which is where most people expect appointments to
appear. Rejected because the cost is an app review, a 100-user cap and an
unverified-app warning until it clears — and it buys write access to *every* event
on *every* calendar the mentor has, to solve a problem a dedicated calendar solves
with no review at all. The strongest argument for it is placement, and placement
is a preference rather than a capability.

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

**Requesting `calendar.events.owned` instead of `calendar.events`.** Narrower, and
still sensitive. It carries the review without avoiding it, so it is the worst of
both — mentioned only because "pick the narrower sensitive scope" is the reflex
this record replaces with "check whether a non-sensitive one does the job".

Mirroring or polling calendar state is **not** reconsidered here. ADR 0004
rejected it on cost and freshness, and nothing in this record touches that
reasoning.

## Consequences

**Session events appear in a separate calendar rather than the mentor's primary
one.** Some mentors will prefer that — EduFurther commitments are visibly grouped
and can be hidden or shared independently. Others will find a second calendar
cluttering. It is the one user-visible cost of this decision and it is not
reversible without moving to a sensitive scope.

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

**A later capability may still need a sensitive scope**, and this record does not
foreclose it. Writing into a mentor's primary calendar, or reading event details
to show a mentor what they are double-booked against, would both require crossing
the tier. That is a decision to take when the capability is actually wanted, with
its own record and its own accounting of the review cost — not a door closed here.

### Confirmation

- **Mechanical, once built:** no sensitive scope appears in either Cloud project's
  configuration. Not checkable from this repository — the scope list lives in the
  Google console, so this is a review step against this record rather than a test.
- **Not mechanical, and the largest gap:** nobody has verified that an event on an
  app-created secondary calendar sends attendee invitations. ADR 0004's mechanism
  for reaching 1,200 mentees without an OAuth flow depends on it, and this record
  is written on documentation rather than on a working flow — the same caveat ADR
  0007 made about adopting a schema on reading.
- **Not mechanical:** nobody has verified that the secondary calendar's busy
  intervals appear in the mentor's own free/busy. If they do not, EduFurther
  sessions will not block against each other, which is a double-booking bug in the
  exact place the feature exists to prevent one.
- **Not mechanical:** nothing detects a scope being added to either project later.
  A sensitive scope added in the console is invisible to CI, to this repository,
  and to everyone except whoever next opens the consent screen.

### Open questions

- **Do attendee invitations send from an app-created calendar?** Testable in
  minutes against a real Google account, and it should be tested before calendar
  work starts rather than after.
- **Does the secondary calendar count toward the mentor's own free/busy**, and what
  is its default visibility?
- **Does `calendar.freebusy` reach a mentor's institutional calendar** — the shared
  one their teaching sits on, which they have access to but do not own? If not,
  availability has a gap exactly where an academic's real commitments live, and
  `calendar.events.freebusy` may be the better of the two after all.
- ~~**Does Composio support a custom auth config pointing at a second Cloud
  project** cleanly?~~ **Moot — see point 6.** Calendar calls Google directly, so
  there is no integration to price. This question is what prompted the
  re-examination: an open cost against a vendor whose only remaining contribution
  was billing for requests we can make ourselves.
