# 31. Paystack first for payments; payouts abroad by hand, behind one port

Date: 2026-11-12

## Status

Proposed. It blocks payments work (decision 11 asked for this record before any
starts) and commits money, so it waits for the owner.

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

Neutral:

- The credits model (decision 308) is unchanged. Whether credits are bought,
  and how paid sessions relate to them, is a product decision for the pricing
  work, not this record.

### Confirmation

- Nothing in `domain/` imports a provider SDK or name. The layer check enforces the
  import half. The name half is checked only by review.
- A webhook test proves that an unsigned event, or one the verify call contradicts,
  leaves the ledger unchanged, and that a repeated event moves it once.
- **Not checked by anything:** that the fees above are still current; that manual
  payouts are actually recorded in the ledger. The second needs an admin flow,
  and until one exists it is a process.

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

## More information

- Decisions 8, 10, 11 and 308 (`project-conventions`). The owner's roadmap
  direction of 2026-11-12. The roadmap artifact theme "Monetization".
- Sources read 2026-11-12, to be re-verified at signing: Flutterwave pricing
  (flutterwave.com/ng/pricing, /gh/pricing, /tz/pricing); Stripe changelog,
  cross-border payouts, 2026-05-27; Paystack fee summaries from third-party
  reviews, because Paystack's own page was not reached.
- Revisit when a non-Paystack market has enough paying users, when manual
  payouts exceed what one operator can do in a day a month, or when the entity's
  country changes.
