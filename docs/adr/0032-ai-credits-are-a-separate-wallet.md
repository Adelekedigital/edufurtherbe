# 32. AI credits are a separate wallet; free tools meter anonymous use; invites earn AI credits, and session credits unlock on attendance

Date: 2026-11-13

## Status

Proposed. It changes a shipped rule (decision 308's levels) and commits a new
currency people can buy, so it waits for the owner with ADR 0031, which carries
the payments rail it relies on.

## Context

Every free tool (the scholarship finder now, Fundable next, then the
eligibility checker and cost estimator) makes AI calls that cost money per use,
and none of them limits anonymous use today. The owner's direction of
2026-11-13:

- limit each tool's free use, then invite people to sign up with a promise of
  AI credits to do more;
- AI credits can be bought, or earned: attending sessions, inviting friends,
  and similar gamified actions;
- **AI credits are never mixed with session credits** (owner, "definitely");
- market the platform's other benefits beside the tool: three free sessions a
  month with mentors who have done it, free AI credits for the tools, and an
  application tracker later.

The owner also answered, the same day:

- **Session credits change:** onboarding completion gives 1 a month
  automatically; a mentee reaches 3 a month by attending a session. Invites
  stop counting toward session credits and earn AI credits instead.
- **The invite promise** is both a free first session and AI credits, stated
  with what the AI credits can be used for.
- **Earned AI credits:** "never expire, with a balance cap" and "reset monthly"
  are both acceptable, so both are configuration options.
- **Bought AI credits never expire.** The owner had credits retired by a
  provider after a year unused and does not want that experience for this
  audience.

Evidence from the legacy app's production export (2026-11-08): 43% of sessions
that should have happened did not, and 74% of mentees who ever booked did so
within a day of signing up. Rewarding attendance targets the platform's largest
problem; meeting people at the moment of sign-up targets its best conversion
window.

## Decision

1. **AI credits are their own wallet.** A separate balance with separate rules,
   shown separately ("Session credits book mentors. AI credits power the
   tools."). Nothing converts between them, and no rule of one reads the other's
   balance. The lot mechanics (expiring and non-expiring lots, an append-only
   ledger, spending the soonest-expiring first) are reused from the credit
   ledger where the code is the same, per rule 8; the balances never are.
2. **A credit is priced from cost.** Each tool action has a credit cost in
   configuration (for example 1 credit for a scholarship search, more for a
   Fundable match and draft), set from what the AI call actually costs. A daily
   ceiling on total AI spend stops a runaway, whatever the balances say.
3. **Anonymous use is metered per browser, with the IP as a backstop.** Each
   tool has a free limit for a visitor who is not signed in, counted against a
   signed browser token, plus a generous per-IP cap and a bot check (for
   example Cloudflare Turnstile) against scraping. **The IP is not the meter**:
   Nigerian mobile networks and campus Wi-Fi put many people behind one IP, so
   an IP limit would lock out strangers.
4. **At the limit, show value, then ask.** The tool shows part of the result
   ("3 of 27 matching scholarships") and offers sign-up with a **starting grant
   of AI credits** and the platform's other benefits. A new account lands on
   matched mentors, not the dashboard, because the first day decides whether
   someone books.
5. **Earning AI credits:**
   - attending a session (decision 295's predicate: the mentee came, including
     when the mentor did not), a set amount, capped each month;
   - a friend you invited **attending their first session**, not just signing
     up, so a fake account earns nothing;
   - optionally writing a review, capped.
   Each amount and cap is a setting.
6. **The invite promise:** the invited friend gets a free first session (the
   onboarding credit below already pays for it) and a starting bonus of AI
   credits, with copy that says what AI credits do. The inviter earns AI
   credits when the friend attends.
7. **Bought AI credits never expire.** Packs are sold in naira on ADR 0031's
   rail, as platform revenue with no mentor share.
8. **Earned and granted AI credits follow a configured policy**, either
   `never` with a balance cap (earning stops at the cap, nothing is taken away)
   or `monthly` (they reset on the 1st, like session credits). **Proposed
   default: `never` with a cap**, which bounds the cost without the loss the
   owner wants to avoid. Spending always uses earned credits before bought
   ones.
9. **Session credits: onboarding gives 1 a month; attending a session unlocks
   3 a month; invites no longer count.** This amends decision 308: level 2 (a
   qualified invite) is removed, and the unlock (decision 295) needs attendance
   alone instead of an invite plus attendance. **Levels still never go down**,
   so a mentee at level 2 today keeps it until they attend, and a mentee at
   level 1 who has already attended moves to 3 when this ships. Grandfathered
   migrated users stay at 3. "Attending" keeps decision 295's meaning (attended,
   not completed), because the owner chose it to avoid penalising a mentee whose
   mentor did not come (owner confirmed, 2026-11-13).

10. **The numbers (owner, 2026-11-13), all in configuration:**
    - anonymous use: **3 per tool per day**;
    - sign-up grant: **20 AI credits**;
    - attending a session: **5**;
    - invites: the **first** invited friend who attends earns **5**; each
      **later** one earns **1** (the owner offered 0.5 or 1; 1 keeps credits
      whole numbers, as the ledger counts them);
    - earned balance cap: **50**.
11. **Configuration is one structured setting per area, not one variable per
    number** (owner: "a json or list set ... gives room for future addition").
    For example `AI_CREDIT_EARN={"session_attended": 5,
    "first_invite_attended": 5, "later_invite_attended": 1}`,
    `AI_CREDIT_LIMITS={"scholarship_finder": {"anonymous_per_day": 3}, ...}`
    and `AI_CREDIT_POLICY={"signup_grant": 20, "earned_expiry": "never",
    "earned_cap": 50, "daily_spend_ceiling_ngn": ...}`. Each is parsed into a
    typed model in `core/config.py`, the way `EMAIL_TEMPLATES` and
    `QSTASH_SCHEDULE_OVERRIDES` already are, so an unknown key or a negative
    amount fails at startup rather than at the first grant. A new earning
    action is a new key and its code, not a new variable.
12. **The reward is told, not just granted** (owner): the message says what
    showing up gets this person ("Attend Thursday's session to unlock 3 free
    sessions a month and earn 5 AI credits"), not a generic "earn rewards".
    It goes in the session reminders and confirmations on the channels the
    person has (in-app, push, email, and WhatsApp once the WhatsApp reminders
    theme ships), and once more when the credits land.

## Consequences

Good:

- The tools' cost is bounded per visitor, per account and per day.
- Sign-up happens at the moment a visitor has just seen value, and the
  invite and attendance rewards push towards the behaviour the platform needs.
- Session credits get one simple rule a mentee can be told: finish onboarding
  for 1 a month, attend a session for 3.

Bad:

- Two currencies can confuse people; the wording in the app has to carry the
  difference every time both appear.
- Removing level 2 changes a shipped rule and its copy: the Invite card, the
  credit card and the referral emails all change, and the frontend must be told
  before the response fields change.
- `never` with a cap leaves an outstanding balance of unspent credits as a cost
  the platform may meet later; the cap is what bounds it.
- A browser token can be cleared to get a fresh free limit. The per-IP cap and
  the bot check limit the damage; the limit is a nudge, not a wall.
- Bought credits are prepaid value. Their refund terms and any tax on them are
  part of ADR 0031's open tax question.

### Confirmation

- No query or rule in the session-credit code reads an AI-credit table, and
  the reverse; a test asserts that spending one never moves the other.
- Tests prove the anonymous limit counts per browser token, that the per-IP cap
  blocks a flood from one address without blocking a second browser behind it
  below the cap, and that the daily spend ceiling refuses an action once
  reached.
- Tests prove an invite earns nothing until the friend attends, that bought
  lots never expire, that the earned policy's cap stops earning and takes
  nothing back, and that spending uses earned lots first.
- Tests prove the new levels: onboarding pays 1, a first attended session lifts
  to 3, an invite alone changes no session level, and no level decreases.
- **Not checked by anything:** that each tool's credit cost still matches its
  real AI cost. That needs the AI spend report reviewed when a model or price
  changes.

## Options considered

- **One wallet for sessions and AI.** Simpler to build. Rejected by the owner:
  session credits are free and never sold, and mixing in bought credits would
  break that rule and the monthly reset.
- **Meter by IP alone.** The simplest anonymous limit. Rejected: carrier NAT
  and shared Wi-Fi would lock out everyone behind a busy address.
- **Expire earned credits after 12 months unused.** Common, and bounds the
  liability. Not chosen as the default: it is the experience the owner wants
  to spare this audience. Not built unless asked.
- **Keep invites in session credits** (decision 308 as shipped). Not chosen:
  the owner wants session access tied to attendance, and invites rewarded with
  AI credits.

## More information

- ADR 0031 (payments rail, PR #672); decisions 295 and 308 (`project-conventions`).
- The monetisation strategy doc and planner (claude.ai artifacts
  CPufFkRoRjA9EnsB9zQE7C and CN2TPMkhUEtNvtEGEdxMx6); the roadmap theme
  "AI credits and free-tool limits".
- Open for the owner: credit pack prices (with ADR 0031's pricing), each
  tool's credit cost once its real AI cost is measured, and the daily spend
  ceiling.
