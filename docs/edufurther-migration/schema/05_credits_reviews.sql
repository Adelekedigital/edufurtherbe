-- =============================================================================
-- 05_CREDITS_REVIEWS
-- Credit lots and ledger, referral gate, reviews.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- credit_lots — replaces User.bookingCredit + bookingCreditRenewDate.
--
-- WHY LOTS AND NOT A COUNTER OR A PERIOD BALANCE
-- ----------------------------------------------
-- An earlier draft used credit_periods with a monthly reset. That assumed ALL
-- credits are free. The platform is moving to paid alongside free, and:
--
--   PAID CREDITS MUST NEVER EXPIRE. Expiring something a user bought is a
--   chargeback and, in several jurisdictions, unlawful.
--
-- Free credits expire monthly; purchased ones don't. A single balance can't
-- represent both lifecycles. A lot is a batch with ONE origin and ONE expiry.
--
-- "New credit clears old, stands at 5" needs no special reset logic — last
-- month's lot expired, this month's granted 5.
--
-- CONSUMPTION IS FIFO WITH EXPIRING-FIRST ORDERING:
--   ORDER BY (expires_at IS NULL), expires_at ASC, granted_at ASC
-- Burn free before paid, soonest-expiring first. What users expect, and it
-- minimises liability.
--
-- REFERRAL GATE (decided):
--   signup, no invite yet  -> signup_baseline, qty 1, never expires, never renews
--   first qualifying invite -> referral_unlock, qty 5, expires end of month
--   each month after        -> monthly_free,    qty 5, expires end of month
--
-- unit_cost_cents and currency sit unused at zero until payments land. That's
-- what makes the payments work additive rather than a rewrite.
-- -----------------------------------------------------------------------------
CREATE TABLE credit_lots (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  source             credit_source NOT NULL,
  quantity_granted   int NOT NULL CHECK (quantity_granted > 0),
  quantity_remaining int NOT NULL CHECK (quantity_remaining >= 0),

  unit_cost_cents    int NOT NULL DEFAULT 0,
  currency           char(3) NOT NULL DEFAULT 'USD',

  expires_at         timestamptz,               -- NULL = never (all paid credits)
  granted_at         timestamptz NOT NULL DEFAULT now(),
  expired_at         timestamptz,               -- set by the expiry job

  ref_type           text,
  ref_id             uuid,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id   text UNIQUE,

  CONSTRAINT remaining_lte_granted CHECK (quantity_remaining <= quantity_granted),
  CONSTRAINT paid_credits_never_expire
    CHECK (source <> 'purchase' OR expires_at IS NULL)
);

CREATE INDEX idx_credit_lots_consumption ON credit_lots
  (user_id, (expires_at IS NULL), expires_at, granted_at)
  WHERE quantity_remaining > 0 AND expired_at IS NULL;
CREATE INDEX idx_credit_lots_expiry ON credit_lots (expires_at)
  WHERE expired_at IS NULL AND expires_at IS NOT NULL;

-- Only one baseline grant per user, ever.
CREATE UNIQUE INDEX idx_credit_lots_one_baseline ON credit_lots (user_id)
  WHERE source = 'signup_baseline';


-- -----------------------------------------------------------------------------
-- credit_transactions — the ledger.
--
-- WHY THIS EXISTS RATHER THAN JUST A BALANCE (asked directly, worth recording):
--
--   1. REFUNDS. Mentee books (-1), mentor cancels, credit returns (+1). With a
--      counter you write balance = balance + 1 and there's no record it happened.
--      "I was charged for a session that never ran" becomes unanswerable.
--   2. CONCURRENCY. UPDATE users SET credits = credits - 1 under two
--      simultaneous bookings is a lost-update race. An append-only insert plus
--      a balance check is atomic and auditable.
--   3. ABUSE DETECTION. "5 credits consumed in 8 minutes across 5 mentors" is a
--      query against transactions. Against a counter it's invisible.
--   4. MONEY. Once credits are purchasable this stops being nice-to-have and
--      becomes accounting — you must reconcile Stripe against internal balances.
--
-- MIGRATION: only OPENING BALANCE entries can be created. Bubble never recorded
-- transaction history, so it cannot be reconstructed.
-- -----------------------------------------------------------------------------
CREATE TABLE credit_transactions (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credit_lot_id  uuid NOT NULL REFERENCES credit_lots(id),
  delta          int NOT NULL CHECK (delta <> 0),   -- negative = consumption
  reason         credit_reason NOT NULL,
  session_id     uuid REFERENCES sessions(id),
  notes          text,
  created_by     uuid REFERENCES users(id),         -- null = system
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_credit_tx_user ON credit_transactions (user_id, created_at DESC);
CREATE INDEX idx_credit_tx_lot ON credit_transactions (credit_lot_id);
CREATE INDEX idx_credit_tx_session ON credit_transactions (session_id)
  WHERE session_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- referrals
--
-- qualified_at is DELIBERATELY separate from signed_up_at. Inviting someone who
-- signs up and vanishes shouldn't unlock credits — that's the abuse boundary.
-- Without it, ten throwaway addresses farm credits forever.
--
-- OPEN DECISION: what counts as "qualifying." Email-verified alone is farmable.
-- RECOMMENDATION: invitee completes onboarding. (Session-completed is stricter
-- but delays the referrer's reward by days, which weakens the loop.)
-- -----------------------------------------------------------------------------
CREATE TABLE referrals (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  referrer_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code            text NOT NULL,
  invitee_email   citext,
  invitee_user_id uuid REFERENCES users(id),
  status          referral_status NOT NULL DEFAULT 'sent',
  invited_at      timestamptz NOT NULL DEFAULT now(),
  signed_up_at    timestamptz,
  qualified_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (referrer_id, invitee_email)
);

CREATE INDEX idx_referrals_code ON referrals (code);
CREATE INDEX idx_referrals_invitee ON referrals (invitee_user_id)
  WHERE invitee_user_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- referral_unlocks — one row per user, because the gate is ONCE-ONLY.
--
-- Makes "has this user unlocked recurring credits" a single indexed lookup
-- instead of an aggregate over referrals, and the PK makes double-unlocking
-- structurally impossible.
-- -----------------------------------------------------------------------------
CREATE TABLE referral_unlocks (
  user_id                 uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  unlocked_by_referral_id uuid NOT NULL REFERENCES referrals(id),
  unlocked_at             timestamptz NOT NULL DEFAULT now(),
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- reviews — legacy Reviews (53 rows). Cleanest table in the dump.
--
-- TWO ADDITIONS: session_id (which session produced this) and a uniqueness
-- constraint preventing duplicates.
--
-- reviewed_role exists because dual roles are supported from day one — a
-- mentor's rating and their behaviour as a mentee shouldn't blend into one score.
--
-- CONTEXT WORTH RECORDING: 53 reviews against 935 sessions is a 5.7% response
-- rate. That's a product problem, not a schema problem, but the new shape makes
-- it measurable.
-- -----------------------------------------------------------------------------
CREATE TABLE reviews (
  id                    uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id            uuid REFERENCES sessions(id),
  reviewed_by           uuid NOT NULL REFERENCES users(id),
  reviewed_for          uuid NOT NULL REFERENCES users(id),
  reviewed_role         session_role NOT NULL DEFAULT 'mentor',

  communication_rating  int CHECK (communication_rating  BETWEEN 1 AND 5),
  knowledge_rating      int CHECK (knowledge_rating      BETWEEN 1 AND 5),
  practicality_rating   int CHECK (practicality_rating   BETWEEN 1 AND 5),
  support_rating        int CHECK (support_rating        BETWEEN 1 AND 5),
  valuable_rating       int CHECK (valuable_rating       BETWEEN 1 AND 5),
  nps_recommend_score   int CHECK (nps_recommend_score   BETWEEN 0 AND 10),

  public_review         text,
  private_review        text,

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  deleted_at            timestamptz,
  legacy_bubble_id      text UNIQUE,

  CONSTRAINT no_self_review CHECK (reviewed_by <> reviewed_for)
);

CREATE UNIQUE INDEX idx_reviews_one_per_session_author
  ON reviews (session_id, reviewed_by) WHERE session_id IS NOT NULL;
CREATE INDEX idx_reviews_for ON reviews (reviewed_for) WHERE deleted_at IS NULL;

SELECT attach_updated_at_triggers();

COMMIT;
