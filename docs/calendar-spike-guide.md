# Running the ADR 0012 calendar spike

Measures the two behaviours ADR 0012 names as untested, plus two the research
turned up as newly doubtful. Nothing is committed and nothing is decided by
running it — it prints what happened.

You need: the Cloud project that already exists, and **two email addresses you
control** (one is the mentor, one is the mentee).

---

## Measured results

Run against real accounts on 2026-08-16. Consent as `digitalcontent31@gmail.com`,
invitee `edufurtherlearning@gmail.com`.

| Question | Answer |
|---|---|
| Q1 attendee receives an invitation, having authorised nothing | **yes** |
| Q2a busy shows on the mentor's **primary** free/busy | **yes, but not immediately** |
| Q2b busy shows when the secondary calendar is named | yes |
| Q3 read the calendar's sharing (`acl.list`) | no — 403 |
| Q3 add the calendar to the mentor's own list | no — 403 |
| Q3 read the **primary** calendar's events | no — 404 |
| Q4 generate a per-session Google Meet link | **yes** |

**Q1 took three runs to answer, and the first two were the test's fault.** They
authenticated as the invitee, and Google never emails somebody their own event.
The script now prints `creator` and `organizer` off the created event and refuses
to leave the case ambiguous. If you are testing this, **consent as one account
and invite a different one.**

**Q2a is eventually consistent, and reading it once will mislead you.** Runs that
query seconds after the write return an empty `busy`; the same window minutes
later returns the interval on both calendars. What the immediate query measures
is Google's indexing lag, not a scoping rule.

That matters beyond the spike: **a booking flow that writes an event and then
checks free/busy to confirm no conflict may read its own write as absent.**
Conflict detection has to happen before the write, or tolerate the lag.

**Q4 needs `conferenceDataVersion=1` on the insert.** Without it the API accepts
the write and silently drops `conferenceData` — indistinguishable from a
permissions refusal if you only read the response.

## The sender is the account that authenticated

The invitation arrives from the **creator** — the Google account whose token made
the call — not from the calendar. The API's `organizer` field names the
app-created calendar, and reading that field alone led to the wrong conclusion
here; what the recipient sees is the creator.

So the sender identity is not configurable, it is *architectural*. Creating the
event from each **mentor's** account makes the mentor the sender. Creating it
from a **platform-owned** account, with mentor and mentee both as attendees,
makes EduFurther the sender and keeps both mailboxes out of it.

The two jobs ADR 0012 gives one grant can be split:

| | scope | whose account |
|---|---|---|
| read the mentor's availability | `calendar.freebusy` | the mentor's |
| create the event and invite | `calendar.app.created` | EduFurther's own |

A platform-owned event also lands on the mentor's **primary** calendar when they
accept, so it becomes visible in their real free/busy to everyone — which is what
Q2a was reaching for. The cost is that a mentor can decline, and the booking flow
has to have an answer for that.


## 1. Pick the right Cloud project

ADR 0012 point 4 says calendar and sign-in live in **separate** projects. Use the
calendar one. If only the sign-in project exists, make a second — adding a
calendar scope to the sign-in project's consent screen is the exact coupling
that ADR guards against, and it would put all 1,200 users behind a limit that
exists for 44 mentors.

## 2. Enable the API and add the scopes

1. **APIs & Services → Library → Google Calendar API → Enable.**
2. **APIs & Services → OAuth consent screen** → External, in **Testing** mode.
3. Add **yourself and the second address** under **Test users**. In Testing mode
   only listed users can consent, which is what keeps this off the verification
   path entirely.
4. Under **Data access**, add exactly these two scopes:

   ```
   https://www.googleapis.com/auth/calendar.app.created
   https://www.googleapis.com/auth/calendar.freebusy
   ```

5. **Write down which heading each one lands under** — "Your non-sensitive
   scopes" or "Your sensitive scopes". This is the tiebreak described below and
   it is the whole reason step 4 is done by hand rather than by the script.

## 3. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID →
Desktop app.** Download the JSON and save it beside the script as
`client_secret.json`.

Desktop app rather than Web: it allows a `localhost` redirect, so the script can
complete the flow without any redirect URI configuration.

## 4. Run it

```bash
cd <the scratchpad directory holding calendar_spike.py>

uv run --with google-auth-oauthlib --with google-api-python-client \
    python calendar_spike.py --attendee the-second-address@example.com
```

A browser opens once. Consent as the **mentor** address. Expect an "unverified
app" warning — that is Testing mode working, not a problem.

## 5. Answer the one question the script cannot

The script prints `?` for whether the invitation actually arrived. Only a human
can answer that: **check the second address's inbox**. An API returning `200`
does not mean an email was sent.

## 6. Clean up

```bash
uv run --with google-auth-oauthlib --with google-api-python-client \
    python calendar_spike.py --cleanup-only
```

Deletes the calendar it made. `calendar_spike_token.json` holds a real refresh
token for your account — delete it too when you are finished.

---

## What each result means

| Result | Consequence |
|---|---|
| **Q1 invitation arrives** | ADR 0004's requirement holds and `calendar.app.created` is sufficient for the write side |
| **Q1 no invitation** | Mentees get nothing. Either send our own ICS — an alternative ADR 0012 rejected — or move to `calendar.events`, which is sensitive |
| **Q2a busy on primary** | Contradicts the documented behaviour below; excellent news, and worth re-reading the docs against |
| **Q2a not busy on primary** | EduFurther sessions are invisible to everyone else. Our own double-booking prevention is unaffected — that is the database constraint — but the mentor can be booked over from outside |
| **Q2b busy on the new calendar** | We can read our own writes back, so our availability computation can use it |
| **Q3 acl.list refused** | Confirms we cannot share the calendar's free/busy without `calendar.acls`, which is sensitive |

## The scope-tier question

Two Google pages disagree, and it decides whether ADR 0012 stands:

- [Choose Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth)
  lists `calendar.app.created` as **Sensitive**.
- [OAuth 2.0 Scopes for Google APIs](https://developers.google.com/identity/protocols/oauth2/scopes)
  lists it as **non-sensitive**.

ADR 0012 says the tiers were read from the Cloud console rather than recalled,
and the console is the operative authority — it is what actually gates
verification. Step 2.5 above is the tiebreak, and it takes a minute.

If it is sensitive, the ADR's central claim — *"there is no review to survive"* —
is wrong, and the decision goes back to whether verification is acceptable.

## Final state — what the spike settled

Measured end to end on 2026-08-16, consumer Gmail accounts throughout.

**A platform-owned account creates the session; the mentor is a guest.**

| | account | scope |
|---|---|---|
| create the event, invite both parties, mint the Meet room | **EduFurther's own** | `calendar.app.created` |
| read the mentor's availability | **the mentor's** | `calendar.freebusy` |

Verified, not reasoned:

* The invitation arrives from the **platform** address. The sender is the account
  whose token made the call — the API's `organizer` field names the calendar and
  is *not* what the recipient sees, which is what led this astray once.
* Both guests joined the Meet room **with the creating account signed out and
  absent**. The calendar-invite bypass applies on consumer accounts, so a session
  needs no host and the platform account never appears.
* The Meet link is minted on the same event the invitation carries, on the same
  two scopes. `conferenceDataVersion=1` is required or it is silently dropped.
* Neither participant's mailbox is used as the sender.

**The mentor's Google connection becomes an enhancement rather than a
prerequisite.** Booking works without it — the platform account owns the event.
What the grant buys is conflict detection against the mentor's real calendar. A
mentor who never connects can still be booked; they can just be double-booked.
That is a materially smaller onboarding requirement than the original design.

### What follows, and is not yet decided

* **The mentor can decline their own session.** They are a guest now, so the
  booking flow needs an answer for a declined mentor, and their acceptance is
  what puts the session on their primary calendar.
* **The OAuth app must be published "In production"** before the platform's
  refresh token is durable. In "Testing" Google issues refresh tokens that expire
  in seven days, so the integration would break weekly.
* **Free/busy is eventually consistent**, so conflicts must be checked *before*
  writing the event, never after.
* **A revoked mentor grant** degrades booking to no conflict checking rather than
  failing it. Whether that is acceptable silently, or must be surfaced, is open.
* The mentee's calendar is not connected, so mentee-side conflicts are invisible.
