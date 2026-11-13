# 31. Payments v1: Paystack first, the platform holds a paid session's money until it happens, payouts abroad by hand

Date: 2026-11-12 (money flow, fee and refunds added 2026-11-13, while still
Proposed)

## Status

Proposed. It blocks payments work (decision 11 asked for this record before any
starts) and commits money, so it waits for the owner. Points 6 to 10 were added
on 2026-11-13 after the owner's answers on monetisation, so the provider and the
money flow are reviewed together.

## Context

Decisions 8, 10 and 11 left payments out of the initial build and set the rules
for when they arrive: money as integer minor units with a currency, mentor
payouts through an append-only ledger paid by hand at first, the rate
snapshotted on the purchase, and the provider undecided because "collections are
African, payouts are global, and the two halves may not share a provider."
The owner's direction of 2026-11-12 asks us to compare African providers with
Africa-friendly global ones and to start with one or two.

**Who pays and who is paid** (counts from the production export of 2026-11-08,
`userprofile.json`, aggregated locally, with no records read out):

- Of the 228 profiles that state an origin country, **184 (81%) are Nigeria**,
  then Ghana 14 and Tanzania 13. The other 684 profiles leave it blank.
- Of the 93 mentors who state a study country, **Nigeria is 44**, the United
  States 14 and the United Kingdom 14, then a long tail.

So collections are overwhelmingly naira, and the first payouts go to Nigerian
bank accounts, with a real minority in the US and UK.

**What the providers offer.** These figures come from public pages and reviews
read on 2026-11-12. They must be re-checked on each provider's own pricing page
when signing:

- **Paystack** (owned by Stripe): merchants in Nigeria, Ghana, Kenya, South
  Africa and Côte d'Ivoire. Nigerian local payments about 1.5% + NGN 100,
  capped at NGN 2,000; international cards about 3.9% + NGN 100. Next-day
  settlement in Nigeria. Transfers (payouts) to local banks and mobile money in
  its countries. Split payments through subaccounts.
- **Flutterwave**: wider African coverage, including Tanzania mobile money.
  Its Nigeria page lists 2% local (with no cap shown) and 4.8% international,
  plus VAT. Flat-fee domestic payouts. Split payments through subaccounts.
- **Stripe**: no direct merchant accounts in Nigeria (an "extended network"
  market routed to Paystack). Cross-border payouts to Nigerian and Ghanaian
  banks were added to Global Payouts in public preview on 2026-05-27. It suits
  a non-African entity paying out globally, not naira collection.
- **Wise and Payoneer**: payouts only. They suit paying a handful of mentors
  abroad by hand.

**What is sold, and what the owner settled on 2026-11-13.** The monetisation
review (production export, aggregates only) found that mentees mostly cannot
pay much: the naira moved from about NGN 460 to about NGN 1,600 per dollar in
2023-24, and the free offer is what works (NPS +85; 74% of mentees who ever
booked did so within a day of signing up). So money comes from mentees or their
parents paying for a **mentor's paid session type**, a discrete high-stakes
service such as a mock visa interview or a statement-of-purpose review, and from
sponsors, never from the free sessions. The owner's answers:

- **Price comes back on session types after payments are integrated**, not
  before. Session Types P2 declined it while nothing could collect it.
- **Credits stay free-only** (the lean recorded on #473): a paid session is paid
  directly, credits are never bought, and payments never touch the monthly
  reset (decision 308).
- **University commissions: yes, later, as a partner, not as an agency.** Out of
  scope here; it gets its own record when it starts.
- **The fee (second answer, 2026-11-13): 15% works for now, but the owner wants
  a regressive commission**, falling as a mentor's monthly sessions rise, never
  below 5%, with flat or regressive chosen in config. Point 8.
- **Refunds follow the credit pattern, with one shift (owner):** a mentee who
  misses a paid session the mentor attended gets one more try within a
  configured window (7 or 14 days). Point 9. Later the same day: when both
  are absent the platform keeps a small fee and refunds the rest (the owner's
  idea), and the fee keeps three modes: flat, lifetime (Preply) and monthly.
- **The entity is Nigerian (owner):** the business is registered in Nigeria and
  goes live on Paystack Nigeria.
- **Tax is to be researched** for the rail, the countries served and the
  country of registration. The starting point is under Consequences.

Sliding commissions are an established pattern, not a new one: Preply charges
tutors 33% falling to 18% by lifetime hours taught; Upwork charged 20%, 10% and
5% by billings per client until it moved to a variable fee; ClassGap tiers by
classes taught in the last year. **Resetting it every month is the less common
part**: these platforms count lifetime or a trailing year.

Comparable marketplaces, from their own help pages read 2026-11-13:
MentorCruise adds a fee of "along the lines of 20%" on top of the mentor's
price; ADPList's paid Advance sessions take a flat 10%.

## Decision

1. **Paystack is the first provider, for collections and for Nigerian payouts.**
   It has the lowest local fee, with a cap, for the 81% who are Nigerian. It
   offers local methods (card, bank transfer and USSD in Nigeria; mobile money
   in Ghana and Kenya), next-day naira settlement, and Transfers to Nigerian
   banks. Being Stripe's African arm keeps a later Stripe step close.
2. **v1 pays out to Nigerian bank accounts only, through Paystack Transfers;
   payouts abroad start by hand once a rail is confirmed.** The entity is
   Nigerian, and Wise appears not to open business accounts for companies with
   a Nigerian address (competitor and third-party sources, 2026-11-13;
   unconfirmed with Wise). So the foreign rail is the owner's to confirm before
   any mentor abroad is paid: Wise if it accepts the entity, otherwise a
   domiciliary account's SWIFT transfer or a provider such as Flutterwave or
   Airwallex. Until then a mentor abroad can sell paid sessions with a Nigerian
   account, or keep offering free ones. Manual payouts are recorded in the
   ledger with the transfer's reference (decision 10) until volume makes the
   manual step the bottleneck; the automated rail gets its own record.
3. **Flutterwave is the named second provider**, added only when a market
   Paystack does not serve (Tanzania mobile money first) has enough paying users
   to justify a second integration. It is not integrated now.
4. **One payments port, no provider in the domain.** `domain/` defines what a
   payment, a refund and a payout are. A `PaymentsPort` in `infra/` has a
   Paystack adapter, so a second provider is a second adapter, not a rewrite.
   The ledger (decision 10) is the truth. The provider's references are stored
   on its rows, never used as our ids.
5. **Every provider event is verified and idempotent.** Webhooks are accepted
   only with a valid signature, then re-confirmed by calling the provider's
   verify endpoint before the ledger moves. Each event is applied once, keyed on
   the provider's event or transaction reference. The same rule holds for the
   Daily webhook today.
6. **What v1 sells is a mentor's paid session type, paid at booking.** A session
   type gains a price (integer minor units and a currency, decision 10), set by
   the mentor (decision 9), in naira first. The price is snapshotted onto the
   payment and the fee rate onto the earning (point 8), so a later change
   rewrites nothing. A paid
   booking is charged when it is requested, because bank transfer and USSD
   cannot be authorised and captured later; the slot is held for a short
   payment window and released if the charge does not complete. Free sessions
   and credits are unchanged. The payer is the mentee's own account in v1;
   paying for someone else (a parent, a relative abroad) is a later step.
7. **The platform collects the whole amount and holds it until the session
   happens; it does not split at the charge.** Paystack's split payments settle
   the mentor's share at once, and then a decline, an expiry or a mentor no-show
   could not be refunded from it. So a captured payment writes a **pending**
   earning to the mentor's ledger. It becomes **payable** only when attendance
   settles with the mentor present, and payouts are batched by hand from payable
   earnings (decision 10; point 2 for mentors abroad).
8. **The platform fee is a percentage taken from the price the mentee sees, set
   by a configured schedule.** The mentor sets the one all-in price the mentee
   pays and is shown what they will receive ("you receive NGN 17,000"); the
   mentee's price never depends on the tier. The platform pays the provider's
   fee from its share.
   - **Three modes, chosen in configuration, each with its own tier list**
     (from this many sessions, this rate), so switching is a setting, not a
     code path (owner, 2026-11-13: keep all three):
     - `flat`: one rate for every session. **The launch mode, at 15%** (owner).
     - `lifetime` (Preply's model, the owner's preference): the mentor's paid
       sessions settled with the mentor present since payments began. It
       rewards long service and never resets.
     - `monthly`: the same count within the calendar month, reset on the 1st.
   - **The floor is 5% in every mode** (owner); configuration refuses a tier
     below it at startup.
   - **What counts:** only paid sessions that settled with the mentor present,
     counted when each settles. Booked or cancelled sessions do not count,
     because they can be inflated and undone. Free sessions do not count.
   - **Each session's rate is its own tier, never back-dated** (the 10th
     counted session is charged the 10th's rate; the first nine keep theirs).
     Back-dating would rewrite earnings already shown to the mentor and
     complicate payouts.
   - **The rate is fixed when the earning becomes payable** (point 7), and the
     mode, tier, rate and schedule version are recorded on that ledger row, so
     a mode switch rewrites nothing already earned.
   - **A minimum fee in naira** (a setting) stops a low price at a low tier from
     costing the platform money once the provider's fee and refunds are paid.
   - Illustrative tiers, for the owner to set: `lifetime` 1-10 at 15%, 11-50 at
     12%, 51-200 at 9%, over 200 at 5%; `monthly` 1-4 at 15%, 5-9 at 12%, 10-19
     at 9%, 20 and up at 5%.
   Worked example at 15% on a NGN 20,000 price: the mentee pays NGN 20,000, the
   mentor receives NGN 17,000, Paystack takes about NGN 400 (1.5% + NGN 100),
   and the platform keeps about NGN 2,600. At 5% the platform keeps about
   NGN 600.
9. **Refunds follow the credit refund rules, so a mentee learns one policy,
   with two shifts for money (owner).** Before the session: full refund on a
   decline, an expiry, a mentor cancellation, or a mentee cancellation 12 hours
   or more ahead; a mentee cancellation under 12 hours pays the mentor, with no
   retry. After it, by who attended:

   | Session | Mentor | Mentee | Outcome |
   |---|---|---|---|
   | Booked | present | present | Mentor paid |
   | Booked | absent | present | Full refund to the mentee |
   | Booked | present | absent | **One more try** (below); mentor paid once it ends |
   | Booked | absent | absent | **Platform keeps a small fee, the rest refunded**; mentor unpaid; no retry |
   | Retry | present | present | Mentor paid, once |
   | Retry | present | absent | Mentor paid; it ends |
   | Retry | absent | present | **Full refund to the mentee**; mentor unpaid for either slot |
   | Retry | absent | absent | Same as both absent: small fee kept, the rest refunded |
   | Retry not booked in the window | | | Mentor paid |

   - **One more try (owner):** one rebooking of the same paid session type with
     the same mentor, at no charge, within a configured window (default 14
     days, settable to 7) from the missed session. One try per payment. The
     mentor's earning stays pending meanwhile and becomes payable at most once.
     A mentor who declines, cancels or lets the rebooking expire is treated as
     absent, and the mentee is refunded in full.
   - **A mentor absent from the retry forfeits the payment**, even after
     attending the first slot. The payment buys one session that happens; the
     mentor promised the retry by accepting it, and a mentee who turned up
     should not pay for a session that never happened. This keeps "mentor
     absent means refund" true in every row.
   - **Both absent (owner's idea, adopted):** the platform keeps a
     **no-show fee** (a setting: a percentage of the price, never less than the
     naira minimum fee) and refunds the rest; the mentor is unpaid. Neither side
     turned up, so neither is made whole, and the fee covers the provider's
     charge, which a refund does not return. Both were absent from 70 of the 370
     no-shows in the old data, so this case is common. **The fee must be shown
     before payment**, with the policy, to be fair and to meet consumer
     protection rules (Nigeria's FCCPA 2018; to confirm with the lawyer).
   Each refund reverses the pending earning in the ledger; nothing is deleted.
   **The policy is customer-facing: it ships as written here only once the
   owner accepts this record.**
10. **Sponsors stay off the payments rail in v1.** A sponsored cohort is invoiced
    outside the platform and delivered through support's credit grants (#224),
    reported from admin analytics. An `invoices` table waits until sponsor
    volume makes the manual step the bottleneck.

## Consequences

Good:

- The cheapest, best-fitting path for most payers, with local methods and next-day
  settlement.
- One integration to build and run. The port keeps the second provider additive.
- No card data touches our systems: hosted checkout or Paystack's inline popup keeps
  us out of PCI scope beyond SAQ A.

Bad:

- **The entity is Nigerian (owner), so Paystack Nigeria can onboard it**; the
  merchant onboarding itself is still the longest lead item.
- Payers outside Paystack's countries can pay only by international card, at
  about 3.9%, and some cards will be refused.
- **Mentors abroad cannot be paid until a foreign rail is confirmed** (point 2),
  and manual payouts abroad cost the operator time and carry fees and currency
  spread. They don't scale past a few mentors a month.
- Pricing in naira against payouts in dollars or pounds puts currency risk on
  the platform between collection and payout. That is a pricing question, not
  a code one.
- **`monthly` rewards bunching**: a mentor near a boundary late in the month
  gains by fitting sessions in before the 1st, and every mentor starts the
  month at the top rate. **`lifetime` is permanent**: a busy mentor reaches the
  5% floor for good, so the floor and the naira minimum carry the margin.
- **The one-more-try rule costs mentors a slot** when a mentee misses, and it
  delays their payout by up to the window. The 44 mentees behind 56% of
  mentee-absent no-shows in the old data are why it is one try per payment.
- **Tax is not settled here; it needs a Nigerian accountant.** The rules below
  are a starting point from accounting-firm summaries, read 2026-11-13, and the
  sources disagree on thresholds:
  - **VAT at 7.5%** under the Nigeria Tax Act 2025. The open question is
    whether the platform charges it on its **fee only** (acting as the mentor's
    agent, the usual marketplace position) or on the **whole price** (acting as
    the seller). That choice decides the receipt's wording and the ledger's
    lines, so it is needed before the build. Registration thresholds are quoted
    as NGN 25m or NGN 50m of turnover.
  - **Company income tax**: small companies pay 0%, with the turnover limit
    quoted as NGN 50m or NGN 100m.
  - **Withholding on payouts**: about 2% for resident service providers and
    5% for non-resident ones under the 2024 regulations (higher for listed
    professional fees). Small suppliers with a tax id are exempt under about
    NGN 2m a month. So the payout ledger must be able to hold a withholding
    line per payout, and Nigerian mentors may need to give a tax id.
  - Every rate and threshold is configuration, never code, so a ruling changes
    a setting.
- **Holding money for mentors until the session (point 7) may be regulated**
  (escrow or marketplace rules for the entity's country). That is a legal check
  for the owner before go-live, alongside the entity itself.
- Refunds may not return the provider's fee, so every refund costs the platform
  about 1.5% + NGN 100. Declines, expiries and mentor no-shows are the cost of
  charging at booking.
- A paid session's money depends on attendance being recorded correctly, so the
  join-window and no-show rules become money rules as well as product ones.

Neutral:

- The credits model (decision 308) is unchanged: credits stay free-only and
  payments never count toward the monthly reset (point 6, #473).
- Decision 18 holds more firmly: with a fee on the booking, mentor and mentee
  conversation stays on the platform, including through any WhatsApp agent.

### Confirmation

- Nothing in `domain/` imports a provider SDK or name. The layer check enforces the
  import half. The name half is checked only by review.
- A webhook test proves that an unsigned event, or one the verify call contradicts,
  leaves the ledger unchanged, and that a repeated event moves it once.
- A ledger test proves that an earning is not payable before attendance
  settles with the mentor present, and that each refund case in point 9
  reverses it with a new row rather than an edit.
- A test proves that changing a session type's price or the fee setting leaves
  existing payments' snapshotted amounts unchanged.
- Fee tests prove `flat` charges every session the same, that `lifetime` and
  `monthly` charge the Nth counted session the Nth tier without back-dating
  (and `monthly` restarts on the 1st), that a tier below 5% is refused at
  startup, and that the naira minimum applies.
- A test per row of point 9's table proves its outcome, including that the
  earning becomes payable at most once, that a mentor absent from the retry
  refunds the mentee in full, and that both absent keeps exactly the no-show
  fee.
- **Not checked by anything:** that the fees above are still current; that manual
  payouts are actually recorded in the ledger. The second needs an admin flow,
  and until one exists it is a process. Nothing checks that the legal and tax
  items under Bad were answered before go-live.

## Options considered

- **Flutterwave first.** It has wider coverage (Tanzania mobile money, more
  corridors) and one provider for more of Africa. It lost on cost and fit for
  the payers we have: the Nigeria page shows 2% with no cap and 4.8%
  international, against Paystack's capped 1.5%, while Nigeria is 81% of known
  origins. It stays the second provider for exactly its strength.
- **Stripe first, with a US entity.** It has the best developer platform and
  global payouts. It lost because it cannot collect naira through local methods,
  and Nigeria is an extended-network market routed to Paystack. Most payers
  would be pushed to international cards. It may still be the right payouts rail
  later, under decision 2.
- **A merchant of record (Paddle, Lemon Squeezy).** They handle tax and
  compliance for us. They lost because they are built for software sales to
  card holders: no naira local methods, high fees, and marketplace payouts to
  mentors are outside what they offer.
- **Two providers from day one.** It gives the widest coverage at once. It lost
  under "build for now": the second integration would serve 13 known users while
  doubling the webhook, reconciliation and support surface.
- **Split the payment at the charge (Paystack subaccounts).** It needs no
  payout step at all. It lost on refunds: the mentor's share has already left
  when a mentor declines, lets the request expire or does not attend, and in
  the production export mentors were absent from 179 sessions and let 81
  requests expire. It may return for mentors with a long reliable record.
- **The fee on top of the mentor's price** (MentorCruise's shape). The mentor's
  number is what they receive, which mentors like. It lost because the mentee
  then pays more than the price the mentor chose, for an audience where price
  decides; point 8 shows one price and tells the mentor their share instead.
- **Only one fee mode.** Simpler to build and explain. Not chosen: the owner
  wants flat, lifetime and monthly all available, and one mode with a tier list
  is the same code as three.
- **A trailing year as a fourth mode** (ClassGap). It rewards recent service
  without a hard monthly reset. Not built now; it is one more counting window
  if `lifetime` makes the floor too permanent.
- **A back-dated monthly rebate** (the whole month re-rated at the tier
  reached). It is the most generous to mentors. It lost because it rewrites
  earnings already shown and paid, which point 7's ledger does not allow.
- **Selling credits.** It reuses the credit ledger. It lost as #473 found:
  bought credits fight the monthly free reset, and a mentee who buys and does
  not use them is charged for credits they would have been given.

## More information

- Decisions 8, 10, 11 and 308 (`project-conventions`). The owner's roadmap
  direction of 2026-11-12. The roadmap artifact theme "Monetization".
- Sources read 2026-11-12, to be re-verified at signing: Flutterwave pricing
  (flutterwave.com/ng/pricing, /gh/pricing, /tz/pricing); Stripe changelog,
  cross-border payouts, 2026-05-27; Paystack fee summaries from third-party
  reviews, because Paystack's own page was not reached.
- Read 2026-11-13: Paystack split payments
  (paystack.com/docs/payments/split-payments); MentorCruise fees
  (help.mentorcruise.com/articles/what-fees-does-mentorcruise-take); ADPList
  Advance fees (intercom.help/adplist-community/en/articles/8716947).
- Issues #473 (paid sessions against credits) and #224 (support credit grants).
- Read 2026-11-13, to be re-verified: Preply commission model
  (help.preply.com/articles/4171383); Upwork fee history (golance.com,
  hubstaff.com); Nigeria Tax Act 2025 summaries (PwC Nigeria sectoral analysis,
  techpoint.africa); Withholding Regulations 2024 (EY, PwC, KPMG Nigeria);
  Wise Business availability (slash.com, worldfirst.com, both competitors).
- Revisit when a non-Paystack market has enough paying users, when manual
  payouts exceed what one operator can do in a day a month, or when the entity's
  country changes.
