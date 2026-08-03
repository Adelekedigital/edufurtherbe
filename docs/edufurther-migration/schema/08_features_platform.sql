-- =============================================================================
-- 08_FEATURES_PLATFORM
-- Vision boards (rebuilt), audit, outbox, idempotency, feature flags.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- vision_boards — legacy VB-Vision Boards (10 rows).
--
-- REBUILD, DON'T MIGRATE THE SHAPE. 25 columns spanning six unrelated feature
-- domains in one flat table. At 10 rows this is effectively greenfield — migrate
-- the ten records' MEANING, by hand.
--
-- BEFORE THE REDESIGN SESSION: run a null-count query on those 10 rows. Half
-- those columns are probably empty in all 10 records, which tells you what the
-- feature actually needs to be. 10 rows suggests a discovery/onboarding problem,
-- not a concept problem — badge-driven milestone completion is a real retention
-- mechanic.
--
-- DERIVED, not stored: numOfSessions, sessionCompletedCount,
-- sessionMinutesCompleted, totalNumberOfSessionToComplete, goalCompletedStatus.
-- -----------------------------------------------------------------------------
CREATE TABLE vision_boards (
  id                     uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentee_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  name                   text,
  statement              text,
  status                 vision_board_status NOT NULL DEFAULT 'active',

  target_completion_date date,
  duration_months        int,

  completed_at           timestamptz,
  paused_at              timestamptz,
  pause_reason           text,
  resumed_at             timestamptz,

  card_share_image_url   text,                  -- MUST be re-hosted off Bubble S3
  cert_share_image_url   text,                  -- MUST be re-hosted off Bubble S3

  created_by             uuid REFERENCES users(id),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  deleted_at             timestamptz,
  legacy_bubble_id       text UNIQUE
);

CREATE INDEX idx_vision_boards_mentee ON vision_boards (mentee_id)
  WHERE deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- vision_board_milestones
--
-- The six legacy feature domains become ROWS, not columns:
--   Country Selection      -> milestone_type 'country_selection'
--   School Selection       -> 'school_selection'   (Dream School, numOfSchools)
--   Program Selection      -> 'program_selection'  (programType, targetFieldOfStudy)
--   Test prep              -> 'test_prep'          (testType, targetScore)
--   Document prep          -> 'document_prep'      (documentType)
--   Interview prep         -> 'interview_prep'     (interviewType)
--   Scholarship            -> 'scholarship'        (fundingType)
--
-- config AS jsonb IS APPROPRIATE HERE specifically because milestone types have
-- genuinely heterogeneous shapes and you're not querying across them. This is
-- the narrow case where jsonb is correct rather than lazy.
-- -----------------------------------------------------------------------------
CREATE TABLE vision_board_milestones (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  vision_board_id uuid NOT NULL REFERENCES vision_boards(id) ON DELETE CASCADE,
  milestone_type  milestone_type NOT NULL,
  config          jsonb NOT NULL DEFAULT '{}',
  target_value    text,
  completed_at    timestamptz,
  sort_order      int NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_vb_milestones_board ON vision_board_milestones (vision_board_id, sort_order);


-- -----------------------------------------------------------------------------
-- audit_log — GENERIC who-did-what.
--
-- SEPARATE FROM session_events, deliberately. session_events is domain state
-- queried constantly by product features and needs a rigid schema. This is the
-- catch-all. Don't collapse them.
--
-- Dot-namespaced actions ('mentor.approved', 'user.role_changed',
-- 'credit.adjusted') allow prefix filtering without a rigid enum.
--
-- before/after as jsonb because the shape varies per entity — one of the rare
-- cases where partial row snapshots are correct rather than lazy.
--
-- WRITE FOR: mentor approval/rejection, role and permission changes, admin
-- credit adjustments, account deletion requests, admin session cancellations,
-- session type changes, any impersonation.
--
-- APPEND-ONLY. Revoke UPDATE and DELETE from the application role:
--   REVOKE UPDATE, DELETE ON audit_log FROM app_role;
-- An audit log you can edit is not an audit log.
--
-- GROWS UNBOUNDED. Not a problem at this scale, but decide retention now:
-- monthly partitioning or cold-storage archival after 12-24 months is far easier
-- to plan than to retrofit at 50M rows.
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  actor_id    uuid REFERENCES users(id),        -- null = system
  actor_type  actor_type NOT NULL DEFAULT 'user',
  action      text NOT NULL,                    -- 'mentor.approved'
  entity_type text NOT NULL,
  entity_id   uuid,
  before      jsonb,
  after       jsonb,
  ip_address  inet,
  user_agent  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_entity ON audit_log (entity_type, entity_id, created_at DESC);
CREATE INDEX idx_audit_log_actor ON audit_log (actor_id, created_at DESC);
CREATE INDEX idx_audit_log_action ON audit_log (action, created_at DESC);


-- -----------------------------------------------------------------------------
-- outbox_events — analytics/webhook dispatch.
--
-- REPLACES the legacy trackedSessionPosthog / sessionTrackedPosthog flags. Those
-- were dispatch bookkeeping leaking into domain tables — the sessions table
-- should not record whether you told an analytics vendor about it.
--
-- A consumer reads session_events (already an append-only stream), emits to
-- PostHog, and records its own cursor here.
-- -----------------------------------------------------------------------------
CREATE TABLE outbox_events (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  event_type    text NOT NULL,
  entity_type   text NOT NULL,
  entity_id     uuid NOT NULL,
  payload       jsonb NOT NULL DEFAULT '{}',
  destination   text NOT NULL DEFAULT 'posthog',
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','skipped')),
  attempts      int NOT NULL DEFAULT 0,
  sent_at       timestamptz,
  error_detail  text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_outbox_events_pending ON outbox_events (created_at)
  WHERE status = 'pending';


-- -----------------------------------------------------------------------------
-- idempotency_keys — NEW.
--
-- A flaky mobile connection retrying a booking POST creates TWO sessions and
-- burns TWO credits. Client sends an Idempotency-Key header; the stored response
-- is returned on replay.
--
-- Cheap now, painful to retrofit after the first duplicate-charge complaint —
-- and mandatory before payments.
-- -----------------------------------------------------------------------------
CREATE TABLE idempotency_keys (
  key           text PRIMARY KEY,
  user_id       uuid REFERENCES users(id) ON DELETE CASCADE,
  endpoint      text NOT NULL,
  request_hash  text NOT NULL,
  response_body jsonb,
  status_code   int,
  locked_at     timestamptz,
  completed_at  timestamptz,
  expires_at    timestamptz NOT NULL DEFAULT (now() + interval '24 hours'),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_idempotency_expiry ON idempotency_keys (expires_at);


-- -----------------------------------------------------------------------------
-- feature_flags — NEW.
--
-- You're rebuilding vision boards, adding session types, and changing the credit
-- model. Being able to ship to 10 mentors before 1,200 users changes how safely
-- you can roll out during a migration.
-- -----------------------------------------------------------------------------
CREATE TABLE feature_flags (
  key              text PRIMARY KEY,
  description      text,
  is_enabled       boolean NOT NULL DEFAULT false,
  rollout_percent  int NOT NULL DEFAULT 0 CHECK (rollout_percent BETWEEN 0 AND 100),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feature_flag_overrides (
  flag_key   text NOT NULL REFERENCES feature_flags(key) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_enabled boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (flag_key, user_id)
);


-- -----------------------------------------------------------------------------
-- search_impressions_suppressed — NEW.
--
-- One row when a mentor WOULD have matched a search's filters but was excluded
-- for being unlisted.
--
-- WHY: the paused-mentor re-engagement problem. Notifications risk pushing
-- mentors from 'paused' to 'hidden', which is worse than doing nothing. Instead
-- this becomes a DASHBOARD STAT: when a paused mentor logs in, they see
-- "12 mentees searched for someone matching your profile while you were paused."
-- Zero notification cost, zero fatigue risk, and it lands exactly when they're
-- already thinking about the platform.
--
-- Doubles as a genuinely useful product metric: how much demand is going unmet
-- because mentors are paused?
--
-- If a nudge is ever added, gate it hard: max one email per 30 days, only above
-- a threshold (~10+ suppressed impressions), one-click opt-out.
-- -----------------------------------------------------------------------------
CREATE TABLE search_impressions_suppressed (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentor_user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  searcher_id        uuid REFERENCES users(id),
  suppression_reason unlisted_reason NOT NULL,
  query_context      jsonb NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_suppressed_mentor ON search_impressions_suppressed
  (mentor_user_id, created_at DESC);

SELECT attach_updated_at_triggers();

COMMIT;
