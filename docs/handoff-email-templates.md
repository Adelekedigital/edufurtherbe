# Handoff: designing and creating the platform's email templates

For the session that owns email template creation. Written 2026-10-03 by the
backend session. The variable reference this relies on is
[`loops-template-variables.md`](loops-template-variables.md); the owner's live
status page is <https://claude.ai/artifact/PtC41gX7nFDvXMLF4Tms8y>.

## The job

Create a **new template for every email the new app sends**, in the new design
system (Claude's design system and/or the `edufurtherfe` tokens), through the
Loops and Emailit MCPs. That covers both:

- **existing emails**, which have a legacy template today, and
- **new emails**, which have none yet.

**Do not edit or delete the legacy templates.** They stay as they are for the
legacy app, which still sends with them. The new app moves to the new templates
by configuration (below), so the two never share a template.

With the owner, decide which emails go through **Loops** and which through
**Emailit**, by importance. A proposed split is below. It's the owner's call.

## How the backend sends an email (what the templates must fit)

- Each email has a **key** (e.g. `session_requested`). Railway's
  `EMAIL_TEMPLATES` maps each key to a provider-prefixed template id:
  `"session_requested": "loops:cmbx…"`. Moving an email to another provider is
  a change to that one value (ADR 0025).
- **Variables are read from the template itself.** Before sending, the backend
  asks the provider which variables the template declares and fills exactly
  those. **Use only the variable names listed below** (or the aliases in the
  reference doc). An unknown name makes the send fail loudly, which is
  deliberate.
- **Values are per recipient.** Dates and times are already rendered in the
  recipient's own timezone, and `sessionTimezone` names that zone. Show it next
  to any time.
- **Links:**
  - `sessionUrl`, `dashboardUrl`, `calendarUrl`, `bookLink` and `reviewLink`
    are full URLs into the app (`https://edufurther-app.vercel.app/...` on dev).
  - Meeting links are **never** in email. They're withheld until the join
    window, so `location` is a label only ("Google Meet", "EduFurther video").
- **Some values can be empty**: the topic, booking message, reason and location
  have fallbacks (see "Values that may be absent" in the reference doc). Word
  templates so an empty `reasonMessage` or `sessionDetail` still reads well, for
  example by putting it in its own block that looks fine empty.
- **Publish the template** before handing over its id. Loops reports no
  variables for an unpublished template, and the backend refuses to send
  those.

## Emailit needs backend work first

**Only a Loops sender exists in the backend today.** Emailit is used only by
Supabase, for sign-in codes, through SMTP configured in the Supabase console.
For any email assigned to Emailit, the backend needs:

1. an Emailit sender adapter (send a template with variables, using an
   idempotency key);
2. routing by the `emailit:` prefix in `EMAIL_TEMPLATES` (designed in ADR 0025,
   not built);
3. variable discovery from Emailit's API if it publishes template variables;
   otherwise a pinned list per template.

Tracked as backend issue #348. So: **create
Emailit templates whenever you like, but tell the backend session which emails
go to Emailit**, so the adapter is built before those ids are switched on.
Until then, Loops ids keep working.

## Every email, what it's for, and what it may use

"Core" means every session email may use: `recipientName`, `mentorName`,
`menteeName`, `sessionDate`, `sessionTime`, `sessionTimezone`, `sessionTopic`,
`sessionDetail`, `location`, `sessionUrl`, `dashboardUrl`.

| Key | When it's sent | Who gets it | Variables | Legacy template today |
|---|---|---|---|---|
| `session_requested` | A mentee requests a session that needs approval | mentor | core + `hours` (hours left to answer) | Session Request |
| `mentor_response_reminder` | Nudge before the request lapses | mentor | core + `hours` | Session Request Reminder |
| `session_booked` | A booking on an offering that confirms itself | both parties | core | Session Confirmation |
| `request_accepted` | The mentor accepts a request | mentee | core | Session Confirmation (shared) |
| `request_declined` | The mentor declines | mentee | core + `reasonTitle`, `reasonMessage` | Declined Request |
| `request_withdrawn` | The mentee withdraws a request | mentor | core + `reasonTitle`, `reasonMessage` | Withdrawn Request |
| `request_expired` | A request lapsed unanswered (shown to users as "Unconfirmed") | both | core | **none** |
| `session_cancelled` | Either party cancels a confirmed session | the other party | core + `cancelInitiator`, `reasonMessage` | Session Canceled |
| `session_reminder` | Before a session | both | core + `intervalTime` (how far ahead) | Session Reminder |
| `session_last_reminder` | Shortly before a session | both | core | Session Last Reminder |
| `session_time_suggested` | The mentor declined or cancelled and suggested another time, held 2 hours for the mentee | mentee | core + the suggested time and when the hold ends. The exact names are in the reference doc once #341 merges. | **none** (new) |
| `session_suggestion_reminder` | 30 min before that hold lapses, if not booked | mentee | as above | **none** (new) |
| `review_requested` | After a completed session (and a nudge a day later) | mentee | `reviewBy`, `reviewFor`, `reviewLink`, `sessionTopic`; `interval` is present only on the nudge | sessionReviewRequest |
| `review_received` | A review was published about the mentor | mentor | `reviewBy`, `reviewFor`, `reviewLink`. **Link to the review, don't quote it** (the author can still edit it for ten minutes). | reviewUpdateToMentors |
| `credits_granted` | Starter, invite or support credits granted | the user | `fName`, `bookLink` | Credit: New users update |
| `credits_renewed` | The monthly credits arrived | the user | `fName`, `bookLink` | Credit renewal |
| `credits_expiring` | Unused credits are about to expire | the user | `fName`, `bookLink`, `creditCount` | Unused credit |
| `mentor_application_received` | A mentor applied | **admins** who decide it, never the applicant | `recipientName`, `mentorName` (the applicant) | **none** |
| `mentor_approved` | An application was approved | the applicant | `recipientName` | **none** |
| `mentor_declined` | An application was declined | the applicant | `recipientName` | **none** |
| `mentor_return_reminder` | A self-paused mentor's return approaching (dated), or a check-in (undated). One template words both. | the mentor | `recipientName`, `daysUntilReturn`, `returnOn`, `daysPaused`, `calendarUrl` | Return reminder |
| `calendar_disconnected` | The mentor's Google Calendar link broke | the mentor | `recipientName`, `calendarUrl` | **none** |

**22 emails; 7 have no template at all.** The reference doc's "Notes per
message" has more on the return reminder's two cases and the review nudge.

## A proposed split by importance (the owner decides)

| Tier | Emails | Why it matters |
|---|---|---|
| **1 — the booking can't work without it** | `session_requested`, `mentor_response_reminder`, `request_accepted`, `session_booked`, `request_declined`, `session_cancelled`, `session_time_suggested`, `session_suggestion_reminder`, `session_reminder`, `session_last_reminder`, `calendar_disconnected` | Time-bound; a late or missing email means a missed session or a lost request |
| **2 — status the user should know** | `request_withdrawn`, `request_expired`, `mentor_application_received`, `mentor_approved`, `mentor_declined` | Important but not time-critical |
| **3 — engagement** | `review_requested`, `review_received`, `credits_granted`, `credits_renewed`, `credits_expiring`, `mentor_return_reminder` | Lifecycle nudges; a delay costs little |

Put tier 1 on whichever provider has the better deliverability and monitoring
for transactional mail. Tiers 2 and 3 can sit on either.

## Handing back

For each template created, send the backend session (or write in
`docs/handoff/` in edufurtherfe, which it reads):

- the **key**, the **provider** (`loops` or `emailit`) and the **template id**;
- confirmation it's **published**;
- any variable you needed that isn't in the list above. Don't invent one in the
  template: the send would fail. Ask, and the backend adds a resolver.

The backend session then updates Railway `EMAIL_TEMPLATES`, the registry in
`loops-template-variables.md`, and the owner's status page together, and
test-sends each new template to the owner.
