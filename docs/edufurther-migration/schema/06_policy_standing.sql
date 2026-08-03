-- =============================================================================
-- 06_POLICY_STANDING
-- Booking limits, penalties, user standing, reports and blocks.
-- Greenfield — no Bubble source data.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- booking_policies — configurable limits, not hardcoded constants.
--
-- These numbers get tuned monthly once real behaviour shows up, so they're data.
-- Resolution order: user override -> role default -> global. Highest priority
-- active match wins.
--
-- THE CONCURRENCY CAP AND THE CREDIT BALANCE ARE INDEPENDENT GATES. A mentee
-- with 5 credits can still be blocked at 2 upcoming sessions. Conflating them
-- means changing one silently changes the other.
-- -----------------------------------------------------------------------------
CREATE TABLE booking_policies (
  id                             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  name                           text NOT NULL,
  scope                          policy_scope NOT NULL,
  scope_ref_id                   uuid,          -- user_id when scope='user'

  max_concurrent_upcoming        int,           -- e.g. 2
  max_bookings_per_mentor_window int,           -- e.g. 1
  per_mentor_window_days         int,           -- e.g. 30
  min_hours_before_start         int,
  max_bookings_per_week          int,

  priority                       int NOT NULL DEFAULT 0,
  is_active                      boolean NOT NULL DEFAULT true,
  effective_from                 timestamptz NOT NULL DEFAULT now(),
  effective_to                   timestamptz,

  created_by                     uuid REFERENCES users(id),
  created_at                     timestamptz NOT NULL DEFAULT now(),
  updated_at                     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_booking_policies_lookup ON booking_policies (scope, scope_ref_id, priority DESC)
  WHERE is_active;

INSERT INTO booking_policies
  (name, scope, max_concurrent_upcoming, max_bookings_per_mentor_window,
   per_mentor_window_days, min_hours_before_start, priority)
VALUES
  ('Global default', 'global', 2, 1, 30, 2, 0);


-- -----------------------------------------------------------------------------
-- ENFORCEMENT MUST BE RACE-SAFE.
--
-- Counting then inserting in separate statements is a check-time-of-use bug: a
-- mentee double-clicking "book" gets three sessions past a limit of two.
--
--   BEGIN;
--   SELECT pg_advisory_xact_lock(hashtext('booking:' || :mentee_id));
--
--   SELECT count(*) FROM sessions
--   WHERE mentee_id = :mentee_id
--     AND status IN ('pending_mentor_approval','confirmed')
--     AND starts_at > now();
--   -- reject if >= max_concurrent_upcoming
--
--   INSERT INTO sessions (...);
--   COMMIT;
--
-- The advisory lock serialises ONE user's bookings without touching anyone
-- else's throughput. Cheap and correct.
-- -----------------------------------------------------------------------------


-- -----------------------------------------------------------------------------
-- booking_attempts — records REJECTIONS, not just successes.
--
-- The table people skip and regret. "How many bookings did we block last month,
-- and why" tells you whether a limit of 2 is protecting mentor capacity or
-- strangling engagement. Without it, a limit that's too tight looks identical
-- to low demand.
-- -----------------------------------------------------------------------------
CREATE TABLE booking_attempts (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentee_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mentor_id           uuid REFERENCES users(id),
  session_type_id     uuid REFERENCES session_types(id),
  requested_start_at  timestamptz,
  outcome             booking_outcome NOT NULL,
  policy_id           uuid REFERENCES booking_policies(id),
  session_id          uuid REFERENCES sessions(id),   -- set when outcome='created'
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_booking_attempts_outcome ON booking_attempts (outcome, created_at DESC);
CREATE INDEX idx_booking_attempts_mentee ON booking_attempts (mentee_id, created_at DESC);


-- -----------------------------------------------------------------------------
-- user_infractions — DECAYING events, not counters.
--
-- expires_at is the key design choice: a no-show in January shouldn't cost
-- someone credits in September. Suggested decay: minor 90d, major 180d,
-- severe never.
--
-- waived_at / waived_by / waiver_reason exist because APPEALS NEED A PATH.
-- People have genuine emergencies and an admin must be able to reverse a bad
-- call with an audit trail.
--
-- DISTINGUISH no_show FROM late_cancellation. Cancelling 3 hours ahead is rude;
-- not turning up wastes the mentor's hour entirely. Different severity — and
-- session_events already carries the timestamps to tell them apart automatically
-- (reason_code plus the gap between created_at and starts_at).
-- -----------------------------------------------------------------------------
CREATE TABLE user_infractions (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type           infraction_type NOT NULL,
  severity       infraction_severity NOT NULL DEFAULT 'minor',
  points         int NOT NULL DEFAULT 1,
  session_id     uuid REFERENCES sessions(id),
  reported_by    uuid REFERENCES users(id),
  notes          text,
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz,                   -- null = never decays
  waived_at      timestamptz,
  waived_by      uuid REFERENCES users(id),
  waiver_reason  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_infractions_active ON user_infractions (user_id, expires_at)
  WHERE waived_at IS NULL;


-- -----------------------------------------------------------------------------
-- user_standing — materialised summary. Recompute nightly and on infraction write.
--
-- Exists so the monthly credit grant job does ONE indexed lookup instead of an
-- aggregate over infractions for 1,200 users.
--
-- CREDIT GATE:
--   status IN ('good','warned')  -> grant monthly_free lot
--   otherwise                    -> skip, write audit_log, NOTIFY THE USER
--
-- Silent credit withholding causes churn. 'warned' should trigger a heads-up
-- notification, not a silent penalty.
-- -----------------------------------------------------------------------------
CREATE TABLE user_standing (
  user_id           uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  status            standing_status NOT NULL DEFAULT 'good',
  active_points     int NOT NULL DEFAULT 0,
  restricted_until  timestamptz,
  last_evaluated_at timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_user_standing_status ON user_standing (status)
  WHERE status <> 'good';


-- -----------------------------------------------------------------------------
-- mentor_blocks — a PRIVATE PREFERENCE, not an accusation.
--
-- A mentor blocking a mentee should NOT automatically create an infraction.
-- Recommended: only escalate when N independent mentors block the same person.
-- -----------------------------------------------------------------------------
CREATE TABLE mentor_blocks (
  mentor_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mentee_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (mentor_id, mentee_id),
  CONSTRAINT no_self_block CHECK (mentor_id <> mentee_id)
);

CREATE INDEX idx_mentor_blocks_mentee ON mentor_blocks (mentee_id);


-- -----------------------------------------------------------------------------
-- user_reports — ADJUDICATED, not auto-applied.
--
-- A mentor report must NOT create an infraction directly. If it did, one
-- annoyed mentor could revoke someone's credits. status='upheld' is what
-- creates the infraction, and a human decides.
-- -----------------------------------------------------------------------------
CREATE TABLE user_reports (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  reporter_id       uuid NOT NULL REFERENCES users(id),
  reported_user_id  uuid NOT NULL REFERENCES users(id),
  session_id        uuid REFERENCES sessions(id),
  category          report_category NOT NULL,
  description       text,
  status            report_status NOT NULL DEFAULT 'open',
  resolved_by       uuid REFERENCES users(id),
  resolution_notes  text,
  resolved_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT no_self_report CHECK (reporter_id <> reported_user_id)
);

CREATE INDEX idx_user_reports_open ON user_reports (created_at)
  WHERE status IN ('open', 'investigating');
CREATE INDEX idx_user_reports_reported ON user_reports (reported_user_id);

SELECT attach_updated_at_triggers();

COMMIT;
