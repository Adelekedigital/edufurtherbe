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
- The platform fee was asked to be clarified; point 8 states the shape, and the
  number is the owner's.

Comparable marketplaces, from their own help pages read 2026-11-13:
MentorCruise adds a fee of "along the lines of 20%" on top of the mentor's
price; ADPList's paid Advance sessions take a flat 10%.

## Decision

1. **Paystack is the first provider, for collections and for Nigerian payouts.**
   It has the lowest local fee, with a cap, for the 81% who are Nigerian. It
   offers local methods (card, bank transfer and USSD in Nigeria; mobile money
   in Ghana and Kenya), next-day naira settlement, and Transfers to Nigerian
   banks. Being Stripe's African arm keeps a later Stripe step close.
2. **Payouts abroad start by hand**, through Wise, exactly as decision 10
   allows, recorded in the ledger as paid with the transfer's reference. This
   lasts until volume makes the manual step the bottleneck. At that point the
   candidate is Stripe Global Payouts, or Flutterwave if more African corridors
   matter. That choice gets its own record.
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
   the mentor (decision 9), in naira first. The price and the fee rate are
   snapshotted onto the payment, so a later change rewrites nothing. A paid
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
8. **The platform fee is a percentage taken from the price the mentee sees.**
   The mentor sets the one all-in price the mentee pays and is shown what they
   will receive ("you receive NGN 17,000"). The platform pays the provider's fee
   from its share. The rate is a setting next to the credit ladder, snapshotted
   on each payment. **Its value is the owner's**; this record proposes 15%,
   between ADPList's 10% and MentorCruise's about 20%. Worked example at 15% on
   a NGN 20,000 price: the mentee pays NGN 20,000, the mentor receives
   NGN 17,000, Paystack takes about NGN 400 (1.5% + NGN 100), and the platform
   keeps about NGN 2,600.
9. **Refunds follow the credit refund rules, so a mentee learns one policy.**
   Full refund on a decline, an expiry, a mentor cancellation, a mentee
   cancellation 12 hours or more ahead, or any session the mentor did not
   attend. A mentee cancellation under 12 hours, or a mentee no-show with the
   mentor present, pays the mentor. Each refund reverses the pending earning in
   the ledger; nothing is deleted. **The policy is customer-facing and needs the
   owner's confirmation before it ships.**
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

- **It needs a Nigerian registered business** (or one in another Paystack
  country) to go live. That is the owner's to confirm, and it may be the
  longest lead item.
- Payers outside Paystack's countries can pay only by international card, at
  about 3.9%, and some cards will be refused.
- Manual payouts abroad cost the operator time and carry Wise's fees and
  currency spread. They don't scale past a few mentors a month.
- Pricing in naira against payouts in dollars or pounds puts currency risk on
  the platform between collection and payout. That is a pricing question, not
  a code one.
- Tax (VAT on fees in Nigeria; withholding or 1099-type obligations on payouts)
  is not settled here.
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
- Revisit when a non-Paystack market has enough paying users, when manual
  payouts exceed what one operator can do in a day a month, or when the entity's
  country changes.
