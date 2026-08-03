# 4. Own the OAuth client, write events, read availability on demand

Date: 2026-08-03

## Status

Accepted

## Context

The legacy Bubble application connects mentor calendars through Composio. On
2026-08-03, every connected account in the Composio dashboard was found to be
**revoked**. Nothing alerted; the state was discovered by opening the dashboard
during an unrelated stack review.

The cause is that the integration uses Composio's **managed auth** — a shared
OAuth application owned by Composio, whose quota and publishing status are shared
across all their customers and visible to none of them. Composio's own
documentation directs users to supply their own credentials for toolkits where
end users see a consent screen, and names Google specifically. Managed auth is a
development path that resembles a production one.

Several facts constrain any replacement:

**Verification attaches to the OAuth client, not to the middleware.** Google
verifies the `client_id` that owns the consent screen. Whether the handshake is
orchestrated by Composio, by Nango, or by our own code, the client is ours to
register and ours to get verified. No integration platform removes this step, so
it cannot be a reason to choose between them.

**Google Calendar scopes are in the *sensitive* tier.** That means brand
verification and app review, but not the third-party security assessment that
*restricted* scopes require. Full Drive access is restricted; `drive.file` is
not. This matters for any later expansion into Google Drive.

**Testing publishing status is not a staging environment.** Google invalidates
refresh tokens for apps in Testing after seven days, and caps the audience at 100
individually-listed test users. An integration parked there breaks on a weekly
cycle.

**Write-only calendar integration is below baseline for a booking product.** If a
mentor books a dentist appointment in their personal calendar and the platform
cannot see it, a mentee books over it and the mentor does not appear. On a
platform whose proposition is that the mentor turns up, that failure is
disproportionately expensive. Reading availability is not an enhancement.

**Reading can be done three ways, and they differ by three orders of magnitude.**
Composio bills per tool call. Against its documented free allowance of 20,000
calls per month, on-demand availability queries cost an estimated 1,000–2,000
calls per month at current booking volume, while polling every mentor at
fifteen-minute intervals would cost roughly 127,000. Both figures are derived
from 44 mentors and the booking volume implied by `docs/bubble-data-model.md`;
neither has been measured, and both should be re-derived once real traffic
exists.

**Only one party to a booking needs a connected calendar.** Scheduling products
in this category have the host connect, and deliver the event to the invitee as
a calendar invitation. Requiring 1,200 mentees to complete an OAuth flow would be
unusual, and would multiply every per-connection cost by twenty-seven.

The alternatives considered were Nango — open source, self-hostable, no per-call
pricing — and direct OAuth against Google with no integration platform at all.

## Decision

**We register and own our own Google OAuth client**, and configure it in Composio
as a custom auth config. We submit it for sensitive-scope verification, and we
publish it to Production rather than leaving it in Testing.

**Calendar is a write target and an on-demand read. It is never polled and never
mirrored into our database.**

- On booking confirmation, cancellation, or reschedule, we write the event.
- When rendering bookable slots and again at confirmation, we query free/busy for
  the relevant window.
- We do not run a scheduled job that reads calendars, and we do not keep a local
  copy of calendar contents.

**Availability remains mentor-declared.** A mentor's declared window is the base
layer; the free/busy result is a mask subtracted from it. The calendar never
creates availability, only removes it.

**Only mentors connect a calendar.** Mentees receive a calendar invitation and
complete no OAuth flow.

**We stay on Composio.** Its Google Calendar toolkit exposes a free/busy action,
so the read pattern above is available without leaving. Nango is recorded here as
the named exit, to be taken if — and only if — one of three things becomes true:

1. Composio's action catalogue cannot express something we need.
2. Custody of mentor OAuth tokens becomes a requirement rather than a preference.
3. We deliberately adopt a polling architecture, at which point flat-rate
   infrastructure beats per-call pricing.

If taken, the shape is a single stateless `nango-server` container against a
**dedicated** Supabase project — never the application database — with Upstash
Redis and dependency-bot-gated image upgrades.

### Rejected alternatives

**Move to Nango now.** Its free self-hosted tier covers exactly what we need
(auth and proxy), and its cost is flat rather than per-call, which is genuinely
better under any polling architecture. It was rejected because at the read
pattern we have chosen, Composio's free allowance is not exhausted, so the
migration would add an operational surface — a container, a credential
datastore, an encryption key whose loss forces every mentor to reconnect — to
save nothing. The strongest argument for it is that Composio holds our users'
OAuth tokens today and we cannot inspect that; if that becomes unacceptable, this
decision should be revisited rather than defended.

**Direct OAuth, no platform.** Roughly two hundred lines and no vendor. It was
rejected because token refresh, revocation handling and encrypted-at-rest storage
are the parts that are tedious rather than hard, and we would be writing them
twice once Microsoft is added. The strongest argument for it is that it is the
only option with no third-party in the credential path at all.

**Polling or mirroring calendar state.** Rejected on cost and on freshness: a
poller is both more expensive and more stale than reading at the moment of
decision. The strongest argument for it is that it would let us notify a mentee
proactively when a mentor's calendar develops a conflict after booking — which
on-demand reads cannot do. If that feature is ever required, it justifies a
polling trigger for that purpose alone, and its cost should be priced then.

## Consequences

Free/busy returns busy intervals without event details, so we learn that a
mentor is unavailable without learning why. This is the correct privacy boundary
for reading someone's personal calendar, and it is a property of the chosen read
primitive rather than a policy we have to enforce separately.

Costs stay in Composio's free allowance at current and foreseeable volume,
because per-call pricing scales with work done rather than with mentor count, and
host-only connections keep connection count equal to mentor count permanently.

**Every mentor must re-authorize.** OAuth tokens issued by Composio's client are
meaningless to ours; they cannot be migrated. This lands acceptably because
settled decision #7 already requires every user to re-establish identity by magic
link at first login after the cutover freeze, so the calendar reconnect is one
extra step in an onboarding that is already happening. It would be a significant
disruption at any other time.

**Verification is queue time we do not control**, measured in weeks. It gates
calendar connect at cutover and is blocked only on registering the client, so it
starts before any implementation work.

**A third party continues to hold mentor OAuth tokens.** We are choosing this
knowingly; trigger 2 above is the exit.

**The free/busy read sits in the booking request path.** It adds latency to slot
rendering and makes booking dependent on Composio and Google being reachable. The
degraded behaviour when the read fails — refuse the booking, or fall back to
declared availability alone — is **not decided here** and must be settled when
the endpoint is built.

**This is not the overbooking control.** The guardrail requiring overbooking to
be prevented by a database constraint rather than an application-level
check-then-insert is unaffected. The two solve different problems: the constraint
prevents two mentees claiming one slot in *our* system and remains authoritative;
the free/busy read detects conflicts in the mentor's *external* calendar and is
advisory. Free/busy must never be treated as the mechanism that prevents double
booking.

Free/busy queries take an explicit time window, so they depend on the guardrail
that times are stored UTC with the mentor's IANA zone in a separate column.

### Confirmation

Partially mechanical, and the gaps are the interesting part.

- **Mechanical:** no vendor SDK may be imported outside `infra/`, enforced by
  `scripts/check_layers.py`. A calendar client reaching into `domain/` fails the
  gate.
- **Mechanical, once built:** the absence of a scheduled calendar-reading job is
  visible in the job definitions. A recurring calendar read would be a reviewable
  addition, not a silent one.
- **Not mechanical:** nothing prevents someone adding a poll, mirroring calendar
  contents into a table, or treating free/busy as the overbooking check. These
  are enforced by review against this record.
- **Not mechanical, and a live gap:** nothing currently alerts when connected
  accounts are revoked. That is precisely how this situation went unnoticed. A
  connection-health check is unbuilt and should be, or the same failure recurs
  silently.

### Open questions

- **Meta's conversation rates are irrelevant here, but Composio's real call
  volume is not.** The 1,000–2,000 calls/month estimate is derived, not measured.
  Re-derive once the booking endpoint exists.
- **Degraded behaviour when the free/busy read fails** is undecided, as above.
- **Microsoft Calendar** is not covered by this record. Whether it goes through
  Composio, Nango or directly is a separate decision.
