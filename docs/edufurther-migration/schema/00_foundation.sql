-- =============================================================================
-- 00_FOUNDATION
-- Extensions, helper functions, enums, lookup tables.
-- No user data. Run first, on every environment.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- EXTENSIONS
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid, digest
CREATE EXTENSION IF NOT EXISTS citext;        -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS btree_gist;    -- REQUIRED for the double-booking
                                              -- exclusion constraint on sessions
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- fuzzy name/institution matching
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Deferred, but create the extension now so enabling matching later is a no-op:
-- CREATE EXTENSION IF NOT EXISTS vector;


-- -----------------------------------------------------------------------------
-- UUIDv7
-- Postgres 18+ ships uuidv7() natively. This shim provides it on 14-17.
-- Time-ordered UUIDs give good index locality and don't leak row counts.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  unix_ts_ms bytea;
  uuid_bytes bytea;
BEGIN
  unix_ts_ms = substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3);
  uuid_bytes = uuid_send(gen_random_uuid());
  uuid_bytes = overlay(uuid_bytes PLACING unix_ts_ms FROM 1 FOR 6);
  -- version 7
  uuid_bytes = set_byte(uuid_bytes, 6, (b'0111' || get_byte(uuid_bytes, 6)::bit(4))::bit(8)::int);
  -- variant 10xx
  uuid_bytes = set_byte(uuid_bytes, 8, (b'10'  || get_byte(uuid_bytes, 8)::bit(6))::bit(8)::int);
  RETURN encode(uuid_bytes, 'hex')::uuid;
END
$$;

COMMENT ON FUNCTION uuid_generate_v7() IS
  'UUIDv7 shim for PG < 18. On PG 18+, replace DEFAULT uuid_generate_v7() with uuidv7().';


-- -----------------------------------------------------------------------------
-- updated_at AUTOMATION
-- Convention: every table carries created_at + updated_at (timestamptz).
-- Junction tables carry created_at only where updated_at is meaningless, but we
-- include both everywhere for uniformity so the CI check stays simple.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$$;

-- Helper to attach the trigger. Called at the end of each migration file.
CREATE OR REPLACE FUNCTION attach_updated_at_triggers()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid
    WHERE c.relkind = 'r'
      AND n.nspname = 'public'
      AND a.attname = 'updated_at'
      AND NOT a.attisdropped
      AND NOT EXISTS (
        SELECT 1 FROM pg_trigger t
        WHERE t.tgrelid = c.oid AND t.tgname = 'trg_set_updated_at'
      )
  LOOP
    EXECUTE format(
      'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON public.%I
       FOR EACH ROW EXECUTE FUNCTION set_updated_at()', r.relname
    );
  END LOOP;
END
$$;

-- CI GUARD: fails the build if any public table is missing timestamps.
-- Run this in your test pipeline; it should return zero rows.
--
--   SELECT c.relname AS table_missing_timestamps
--   FROM pg_class c
--   JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE c.relkind = 'r' AND n.nspname = 'public'
--     AND (NOT EXISTS (SELECT 1 FROM pg_attribute a
--                      WHERE a.attrelid = c.oid AND a.attname = 'created_at'
--                        AND NOT a.attisdropped)
--       OR NOT EXISTS (SELECT 1 FROM pg_attribute a
--                      WHERE a.attrelid = c.oid AND a.attname = 'updated_at'
--                        AND NOT a.attisdropped));


-- =============================================================================
-- ENUMS
-- Rule applied throughout: a value goes in an ENUM only if adding a new value
-- would require a code change anyway. Otherwise it goes in a lookup table.
-- =============================================================================

-- Identity ---------------------------------------------------------------------
CREATE TYPE primary_role       AS ENUM ('mentee', 'mentor');
CREATE TYPE admin_role         AS ENUM ('super_admin', 'mentor_approval', 'limited_access');
CREATE TYPE auth_provider      AS ENUM ('google', 'linkedin');
CREATE TYPE auth_code_purpose  AS ENUM ('login', 'email_verify', 'phone_verify',
                                        'email_change', 'phone_change');
CREATE TYPE delivery_channel   AS ENUM ('in_app', 'email', 'push', 'whatsapp', 'sms');

-- Profiles ---------------------------------------------------------------------
CREATE TYPE listing_status     AS ENUM ('listed', 'unlisted');
CREATE TYPE unlisted_reason    AS ENUM ('mentor_paused', 'admin_review', 'dormant',
                                        'never_approved');
CREATE TYPE approval_status    AS ENUM ('pending', 'approved', 'declined');
CREATE TYPE verification_status AS ENUM ('unverified', 'pending', 'verified', 'rejected');
CREATE TYPE language_proficiency AS ENUM ('native', 'fluent', 'conversational', 'basic');
CREATE TYPE scholarship_relationship AS ENUM ('awarded', 'applied', 'advised');
CREATE TYPE lookup_status      AS ENUM ('approved', 'pending_review', 'merged', 'rejected');

-- Sessions ---------------------------------------------------------------------
CREATE TYPE session_status     AS ENUM (
  'pending_mentor_approval',
  'confirmed',
  'completed',
  'cancelled',
  'declined',
  'expired',
  'no_show'
);

CREATE TYPE session_role       AS ENUM ('mentor', 'mentee', 'observer');
CREATE TYPE attendance_status  AS ENUM ('pending', 'attended', 'no_show', 'left_early');

CREATE TYPE session_reason_code AS ENUM (
  'mentor_unavailable',
  'mentee_no_longer_needed',
  'scheduling_conflict',
  'technical_issue',
  'mentor_no_show',
  'mentee_no_show',
  'expired_no_response',
  'rescheduled',
  'admin_action'
);

CREATE TYPE meeting_provider   AS ENUM ('google_meet', 'daily', 'zoom', 'custom');
CREATE TYPE question_type      AS ENUM ('free_text', 'file_upload', 'multi_choice');
CREATE TYPE intake_status      AS ENUM ('draft', 'submitted', 'reviewed');

-- Credits ----------------------------------------------------------------------
CREATE TYPE credit_source      AS ENUM (
  'signup_baseline',      -- 1 credit, never expires, granted once at signup
  'referral_unlock',      -- 5 credits, granted on first qualifying invite
  'monthly_free',         -- 5 credits, granted monthly once unlocked
  'purchase',             -- paid; never expires
  'refund',
  'admin_grant',
  'promotional'
);

CREATE TYPE credit_reason      AS ENUM (
  'grant',
  'session_booked',
  'session_cancelled_refund',
  'session_no_show_forfeit',
  'lot_expired',
  'admin_adjustment',
  'purchase'
);

CREATE TYPE referral_status    AS ENUM ('sent', 'signed_up', 'qualified', 'expired', 'rejected');

-- Policy / standing ------------------------------------------------------------
CREATE TYPE policy_scope       AS ENUM ('global', 'role', 'user');

CREATE TYPE booking_outcome    AS ENUM (
  'created',
  'rejected_concurrency',
  'rejected_no_credit',
  'rejected_mentor_limit',
  'rejected_notice',
  'rejected_standing',
  'rejected_slot_taken',
  'rejected_blocked'
);

CREATE TYPE infraction_type    AS ENUM ('no_show', 'late_cancellation', 'mentor_report',
                                        'terms_violation');
CREATE TYPE infraction_severity AS ENUM ('minor', 'major', 'severe');
CREATE TYPE standing_status    AS ENUM ('good', 'warned', 'restricted', 'suspended');
CREATE TYPE report_category    AS ENUM ('no_show', 'inappropriate', 'spam',
                                        'misrepresentation', 'other');
CREATE TYPE report_status      AS ENUM ('open', 'investigating', 'upheld', 'dismissed');

-- Messaging --------------------------------------------------------------------
CREATE TYPE conversation_status AS ENUM ('pending', 'accepted', 'declined', 'blocked');
CREATE TYPE message_type        AS ENUM ('text', 'attachment', 'system', 'action_card');
CREATE TYPE scan_status         AS ENUM ('pending', 'clean', 'infected', 'skipped');

-- Availability -----------------------------------------------------------------
CREATE TYPE availability_exception_type AS ENUM ('block', 'override');
CREATE TYPE calendar_provider   AS ENUM ('composio_google', 'composio_outlook', 'google_direct');
CREATE TYPE connection_status   AS ENUM ('active', 'expired', 'revoked', 'error');

-- Vision boards ----------------------------------------------------------------
CREATE TYPE vision_board_status AS ENUM ('active', 'paused', 'completed', 'abandoned');
CREATE TYPE milestone_type      AS ENUM (
  'country_selection', 'school_selection', 'program_selection',
  'test_prep', 'document_prep', 'interview_prep', 'scholarship'
);

-- Platform ---------------------------------------------------------------------
CREATE TYPE actor_type          AS ENUM ('user', 'admin', 'system', 'api');
CREATE TYPE deletion_type       AS ENUM ('soft', 'anonymize');
CREATE TYPE legal_document_type AS ENUM ('terms_of_service', 'privacy_policy',
                                         'mentor_agreement', 'community_guidelines');


-- =============================================================================
-- LOOKUP TABLES
-- Values that change with BUSINESS, not code.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- countries — ISO 3166-1. Seeded once; effectively static.
-- Country codes are stored as char(2) on other tables WITHOUT an FK, to avoid
-- coupling every table to this one. Use it for display names and ordering.
-- -----------------------------------------------------------------------------
CREATE TABLE countries (
  code           char(2) PRIMARY KEY,           -- ISO 3166-1 alpha-2
  code_alpha3    char(3) NOT NULL,
  display_name   text    NOT NULL,
  region         text,
  is_featured    boolean NOT NULL DEFAULT false,
  sort_order     int     NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- languages — ISO 639-3.
--
-- WHY 639-3 AND NOT 639-1: the two-letter set covers ~184 languages and omits
-- Nigerian Pidgin entirely, which matters for this platform's market.
--
-- SOURCE: SIL publishes the complete code set as tab-delimited UTF-8, free, no
-- API key: https://iso639-3.sil.org/code_tables/download_tables
-- Filter to Scope IN ('I','M') AND Language_Type = 'L' -> ~7,000 living languages.
-- Commit the resulting seed as a versioned migration. Re-check annually.
--
-- is_featured drives the picker: nobody should scroll 7,000 options to find Yoruba.
-- -----------------------------------------------------------------------------
CREATE TABLE languages (
  code_639_3     char(3) PRIMARY KEY,
  code_639_1     char(2),                       -- null where none exists (e.g. pcm)
  display_name   text    NOT NULL,
  native_name    text,                          -- hand-filled for featured set only
  is_featured    boolean NOT NULL DEFAULT false,
  sort_order     int     NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_languages_featured ON languages (sort_order) WHERE is_featured;

-- -----------------------------------------------------------------------------
-- institutions — REGISTRY, not a mirror.
--
-- Autocomplete is served live from the Hipolabs university-domains-list (already
-- in use). We do NOT cache that catalogue. Rows land here only when a user
-- actually selects an institution — expect ~200-400 rows from 940 education
-- entries, not 9,000.
--
-- WHY A TABLE AT ALL (rather than storing the string):
--   - country derives once at write, not via API call on every profile render
--   - Hipolabs is a static GitHub JSON file, not a versioned API with an SLA;
--     historical education data shouldn't depend on that repo staying up
--   - "mentors who studied in the UK" becomes a join, not a runtime API fan-out
--   - FK integrity: an education entry can't reference a school that isn't here
--   - Hipolabs is incomplete for African institutions; source='manual' fills gaps
--
-- domain is the natural key: names change, domains rarely do.
-- -----------------------------------------------------------------------------
CREATE TABLE institutions (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  name           text    NOT NULL,
  domain         text    UNIQUE,                -- null only for source='manual'
  country_code   char(2) NOT NULL REFERENCES countries(code),
  alt_names      text[]  NOT NULL DEFAULT '{}',
  web_page       text,
  source         text    NOT NULL DEFAULT 'hipolabs'
                 CHECK (source IN ('hipolabs', 'manual', 'ror')),
  status         lookup_status NOT NULL DEFAULT 'approved',
  merged_into_id uuid REFERENCES institutions(id),
  usage_count    int     NOT NULL DEFAULT 0,
  created_by     uuid,                          -- FK added in 01_identity
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_institutions_country ON institutions (country_code);
CREATE INDEX idx_institutions_name_trgm ON institutions USING gin (name gin_trgm_ops);
CREATE INDEX idx_institutions_pending ON institutions (usage_count DESC)
  WHERE status = 'pending_review';

-- -----------------------------------------------------------------------------
-- degree_levels — replaces Members Goals.degreeGoal(text).
--
-- Free text meant "Masters" / "masters" / "MSc" / "Master's Degree" all coexisted
-- across 720 rows and no filter worked. Expect a messy mapping pass in M2;
-- unmappable values go to mentee_goals.degree_goal_raw rather than being dropped.
-- -----------------------------------------------------------------------------
CREATE TABLE degree_levels (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug           text UNIQUE NOT NULL,
  display_name   text NOT NULL,
  sort_order     int  NOT NULL DEFAULT 0,
  is_active      boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

INSERT INTO degree_levels (slug, display_name, sort_order) VALUES
  ('undergraduate', 'Undergraduate',   10),
  ('diploma',       'Diploma',         20),
  ('masters',       'Masters',         30),
  ('mba',           'MBA',             40),
  ('phd',           'PhD',             50),
  ('postdoc',       'Postdoctoral',    60);

-- -----------------------------------------------------------------------------
-- service_offerings — THE SHARED VOCABULARY.
--
-- This is the fix for why matching doesn't work today: Bubble stored
-- "Mentorship Goals" (mentee) and "Mentor Services/Support" (mentor) as two
-- SEPARATE option sets with no mapping between them. Both sides now reference
-- this one table, so overlap is a join.
--
-- This table is also the seed for `tags` when AI matching is built (DEFERRED):
-- service_offerings -> tags WHERE type='mentorship_need'
-- the two junctions  -> user_tags WHERE relation IN ('offers','seeks')
-- -----------------------------------------------------------------------------
CREATE TABLE service_offerings (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug           text UNIQUE NOT NULL,
  display_name   text NOT NULL,
  category       text,                          -- application | test | funding | career
  sort_order     int  NOT NULL DEFAULT 0,
  is_active      boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- scholarship_programs
--
-- DECISION (revised): scholarship_program_id on user_scholarship_experience is
-- NOT NULL. A user-typed custom value always creates a row here with
-- status='pending_review', rather than living as free text on the experience row.
-- Simpler queries, one code path — at the cost of needing a merge mechanism.
--
-- MERGE PATH is mandatory, not optional. Without it you get "Chevening",
-- "chevening scholarship", "Chevening Award" as three rows within a month and
-- filtering by scholarship stops working.
--   1. Suggest-before-create: trigram match on input, "did you mean Chevening?"
--   2. Admin merge: set merged_into_id, repoint references, status='merged'.
--      Keep the merged row so cached client references still resolve.
--
-- usage_count is the admin work queue signal: pending + 8 users = approve;
-- pending + 1 user + a typo = merge.
-- -----------------------------------------------------------------------------
CREATE TABLE scholarship_programs (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug           text UNIQUE,
  display_name   text NOT NULL,
  country_code   char(2) REFERENCES countries(code),
  funding_type   text,                          -- full | partial | tuition_only | stipend
  degree_levels  text[] NOT NULL DEFAULT '{}',
  official_url   text,
  status         lookup_status NOT NULL DEFAULT 'approved',
  merged_into_id uuid REFERENCES scholarship_programs(id),
  usage_count    int  NOT NULL DEFAULT 0,
  created_by     uuid,                          -- FK added in 01_identity
  approved_at    timestamptz,
  approved_by    uuid,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_scholarship_name_trgm
  ON scholarship_programs USING gin (display_name gin_trgm_ops);
CREATE INDEX idx_scholarship_pending
  ON scholarship_programs (usage_count DESC) WHERE status = 'pending_review';

-- -----------------------------------------------------------------------------
-- legal_documents — Terms agreed date recorded WHEN, not WHAT.
-- With payments coming, you need to know who accepted which version.
-- -----------------------------------------------------------------------------
CREATE TABLE legal_documents (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  type           legal_document_type NOT NULL,
  version        text NOT NULL,
  content_url    text NOT NULL,
  effective_from timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (type, version)
);

SELECT attach_updated_at_triggers();

COMMIT;
