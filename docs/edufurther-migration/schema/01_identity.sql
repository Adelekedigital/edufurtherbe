-- =============================================================================
-- 01_IDENTITY
-- Splits the legacy `User` table (1,200 rows, ~30 columns) into focused tables.
--
-- CRITICAL PHASE. Everything downstream depends on this. Reconcile completely
-- before starting 02.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- users — IDENTITY AND AUTH ONLY.
--
-- Legacy `User` was doing five jobs at once: identity, profile, OAuth creds,
-- billing/credits, and onboarding state. Everything not strictly identity has
-- moved out.
--
-- primary_role IS A UX HINT, NEVER AUTHORIZATION.
-- --------------------------------------------------
-- Authorization comes from PROFILE EXISTENCE, not this column:
--   can be booked  -> EXISTS (mentor_profiles WHERE approval_status='approved')
--   can book       -> EXISTS (mentee_goals)
--
-- This is what makes dual roles (mentor who is also a mentee) work from day one
-- at zero cost. A `role` column that gates permissions has to stay consistent
-- with the profile tables and can silently disagree with them; profile existence
-- cannot. primary_role only decides which dashboard someone lands on.
--
-- NEVER write `WHERE primary_role = 'mentor'` in an authorization check.
-- -----------------------------------------------------------------------------
CREATE TABLE users (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),

  email               citext NOT NULL,
  email_verified_at   timestamptz,

  -- NEW: absent from the entire Bubble dump. Required for WhatsApp delivery and
  -- OTP login. Existing 1,200 users need a collection campaign — WhatsApp
  -- reaches nobody whose number you don't have.
  phone_e164          text,                      -- normalise on write, E.164 only
  phone_verified_at   timestamptz,
  phone_country_code  char(2),                   -- display formatting only

  -- Optional. Primary auth is OTP (see auth_codes). Admin accounts should
  -- probably still have a password plus MFA rather than a single OTP channel.
  password_hash       text,

  first_name          text,
  last_name           text,
  -- legacy `First and Last Name` is DROPPED: derived, formatted in the API layer

  primary_role        primary_role NOT NULL DEFAULT 'mentee',
  timezone            text NOT NULL DEFAULT 'UTC',   -- IANA, e.g. Africa/Lagos
  last_active_at      timestamptz,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,

  legacy_bubble_id    text UNIQUE,

  CONSTRAINT phone_is_e164 CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$')
);

-- THE PARTIAL UNIQUE INDEX PEOPLE FORGET.
-- Without `WHERE deleted_at IS NULL`, a soft-deleted user permanently blocks
-- their own email from re-registering.
CREATE UNIQUE INDEX idx_users_email_live ON users (email) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_users_phone_live ON users (phone_e164)
  WHERE deleted_at IS NULL AND phone_e164 IS NOT NULL;
CREATE INDEX idx_users_last_active ON users (last_active_at DESC) WHERE deleted_at IS NULL;

COMMENT ON COLUMN users.primary_role IS
  'UX hint only — which dashboard to land on. NEVER use for authorization; check profile existence instead.';


-- Deferred FKs from 00_foundation now that users exists
ALTER TABLE institutions
  ADD CONSTRAINT fk_institutions_created_by FOREIGN KEY (created_by) REFERENCES users(id);
ALTER TABLE scholarship_programs
  ADD CONSTRAINT fk_scholarship_created_by  FOREIGN KEY (created_by) REFERENCES users(id),
  ADD CONSTRAINT fk_scholarship_approved_by FOREIGN KEY (approved_by) REFERENCES users(id);


-- -----------------------------------------------------------------------------
-- user_profiles — legacy PersonalInfo (858 rows), merged.
--
-- user_id IS THE PRIMARY KEY, not a surrogate id. Since this is strictly 1:1
-- there is no reason for a separate id, and it removes the mentor_profile_id
-- vs user_id ambiguity entirely: they are the same value everywhere.
--
-- THREE COUNTRIES, three meanings:
--   origin_country_code   — nationality / where they're from
--   current_country_code  — where they live NOW  [NEW: absent from Bubble]
--   study country         — lives on education_entries, per degree (see 02)
-- A fourth (target country) is aspirational and lives on mentee_goal_countries.
--
-- current_country_code matters: a Nigerian mentee living in the UK has different
-- visa questions, timezone, and scholarship eligibility than one in Lagos.
-- Starts null; collect at next profile edit or via onboarding prompt.
-- -----------------------------------------------------------------------------
CREATE TABLE user_profiles (
  user_id                   uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

  avatar_url                text,               -- MUST be re-hosted off Bubble S3
  banner_url                text,               -- MUST be re-hosted off Bubble S3
  about_me                  text,
  gender                    text,

  origin_country_code       char(2) REFERENCES countries(code),
  current_country_code      char(2) REFERENCES countries(code),

  social_linkedin           text,
  social_twitter            text,
  social_youtube            text,

  email_provider_contact_id text,               -- legacy emailitContact_id

  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id          text UNIQUE
);

CREATE INDEX idx_user_profiles_origin  ON user_profiles (origin_country_code);
CREATE INDEX idx_user_profiles_current ON user_profiles (current_country_code);

-- Full-text + fuzzy search over names and bios. This plus the mentor_profiles
-- indexes is what replaces the `Mentor (front search)` table entirely — at 44
-- mentors a 6-way join is sub-millisecond; you'd need ~10,000+ before this
-- needs help, and the fix then is a MATERIALIZED VIEW, not a new service.
CREATE INDEX idx_user_profiles_about_fts ON user_profiles
  USING gin (to_tsvector('english', coalesce(about_me, '')));


-- -----------------------------------------------------------------------------
-- auth_identities — external OAuth providers only.
--
-- Legacy `Registration format` was a single option set, so a user who signed up
-- with Google could never also link LinkedIn. This fixes that, and matters more
-- with OTP-primary auth: users will commonly have OTP + Google + LinkedIn.
--
-- Also handles: account linking on email collision (insert against a unique
-- constraint, not a column update), and unlinking (DELETE, not a five-field
-- nullable update with no audit trail).
-- -----------------------------------------------------------------------------
CREATE TABLE auth_identities (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider         auth_provider NOT NULL,
  provider_user_id text NOT NULL,
  linked_at        timestamptz NOT NULL DEFAULT now(),
  last_used_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_user_id)
);

CREATE INDEX idx_auth_identities_user ON auth_identities (user_id);


-- -----------------------------------------------------------------------------
-- auth_codes — OTP login. Replaces password_resets entirely.
--
-- DECISION: 6-digit OTP over magic link. Avoids two real problems — Outlook
-- Safe Links prefetching and consuming magic-link tokens before the human
-- clicks, and WhatsApp's in-app browser opening links in a different session
-- from the user's real browser.
--
-- THE TRADE-OFF: 6 digits is brute-forceable. A million combinations is not
-- many when scripted. The following are NOT optional:
--
--   1. HASH the code (SHA-256). A DB read must not grant login.
--   2. LOCK after max_attempts wrong guesses — invalidate the CODE, not just
--      reject that guess. Otherwise unlimited tries against a live code.
--   3. INVALIDATE previous codes on new request. 100 live codes against a 1M
--      space shifts the odds meaningfully. One active code per (user, purpose).
--   4. 10-MINUTE expiry. Shorter than a magic link's 15, since the code is weaker.
--   5. RATE LIMIT per destination (3 per 15 min), not per IP — per-IP is easy to
--      evade and punishes shared connections, which matters in these markets.
--   6. CONSTANT-TIME comparison. Use timingSafeEqual, not ==.
--   7. UNIFORM response and timing whether or not the account exists.
--   8. CSPRNG generation: crypto.randomInt(100000, 999999), not Math.random().
--
-- Verify and consume are SEPARATE atomic statements:
--   UPDATE auth_codes SET attempt_count = attempt_count + 1
--   WHERE id=? AND consumed_at IS NULL AND invalidated_at IS NULL
--     AND expires_at > now() AND attempt_count < max_attempts
--   RETURNING code_hash;                       -- compare in app, then:
--   UPDATE auth_codes SET consumed_at = now() WHERE id=? AND consumed_at IS NULL;
--
-- WhatsApp OTP templates fall under Meta's `authentication` category and need
-- pre-approval — submit early. Note Nigeria is one of nine markets with a higher
-- authentication-international rate when the WABA is registered elsewhere.
-- -----------------------------------------------------------------------------
CREATE TABLE auth_codes (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id         uuid REFERENCES users(id) ON DELETE CASCADE,
  purpose         auth_code_purpose NOT NULL,
  channel         delivery_channel  NOT NULL,
  destination     text NOT NULL,                 -- email or E.164 phone
  code_hash       text NOT NULL,                 -- SHA-256, never plaintext
  expires_at      timestamptz NOT NULL,
  consumed_at     timestamptz,
  invalidated_at  timestamptz,
  attempt_count   int NOT NULL DEFAULT 0,
  max_attempts    int NOT NULL DEFAULT 5,

  -- requested_ip is for: (a) rate limiting — "20 requests from one IP across 15
  -- emails in 5 minutes" is enumeration; (b) takeover forensics — requested in
  -- Lagos, consumed in Amsterdam 30s later; (c) REFERRAL FRAUD — "15 accounts
  -- from one IP each qualifying a referral" is the abuse pattern you'll actually
  -- see once credits are tied to invites.
  -- GDPR: personal data. Purge rows older than 90 days (see retention jobs).
  requested_ip    inet,
  requested_user_agent text,

  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_auth_codes_lookup ON auth_codes (destination, purpose, created_at DESC);
CREATE UNIQUE INDEX idx_auth_codes_one_active ON auth_codes (user_id, purpose)
  WHERE consumed_at IS NULL AND invalidated_at IS NULL;
CREATE INDEX idx_auth_codes_expiry ON auth_codes (expires_at) WHERE consumed_at IS NULL;


-- -----------------------------------------------------------------------------
-- user_onboarding — 1:1, keyed on user_id.
-- Legacy had BOTH `registration completed` and `Registration completed (Y/N)`;
-- one is dropped.
-- -----------------------------------------------------------------------------
CREATE TABLE user_onboarding (
  user_id          uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  last_step        text,
  completed_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- user_legal_consents — replaces `Terms agreed date`.
-- Records WHAT was accepted, not just when.
-- -----------------------------------------------------------------------------
CREATE TABLE user_legal_consents (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  legal_document_id  uuid NOT NULL REFERENCES legal_documents(id),
  consented_at       timestamptz NOT NULL DEFAULT now(),
  ip_address         inet,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, legal_document_id)
);


-- -----------------------------------------------------------------------------
-- admin_users — real RBAC.
--
-- Legacy put the admin option set on the User table, so there was no way to
-- revoke admin access with an audit trail. Only actual admins get rows here.
-- -----------------------------------------------------------------------------
CREATE TABLE admin_users (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  admin_role   admin_role NOT NULL,
  granted_by   uuid REFERENCES users(id),
  granted_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz,
  revoked_by   uuid REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_admin_users_active ON admin_users (user_id, admin_role)
  WHERE revoked_at IS NULL;


-- -----------------------------------------------------------------------------
-- user_languages — SPOKEN languages (English, French, Yoruba, Swahili...).
--
-- Attached to USERS, not mentor_profiles: mentee language matters for matching
-- too, and PersonalInfo already held Language/list-Language for all users.
-- The legacy `Mentor (front search).mentorLanguages` was a duplicate copy and
-- is dropped — that duplication is exactly what made the front-search table
-- untrustworthy.
-- -----------------------------------------------------------------------------
CREATE TABLE user_languages (
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  language_code char(3) NOT NULL REFERENCES languages(code_639_3),
  proficiency   language_proficiency NOT NULL DEFAULT 'fluent',
  is_primary    boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, language_code)
);

CREATE INDEX idx_user_languages_lang ON user_languages (language_code);
CREATE UNIQUE INDEX idx_user_languages_one_primary ON user_languages (user_id)
  WHERE is_primary;


-- -----------------------------------------------------------------------------
-- account_deletion_requests — user chooses soft vs full erasure.
--
-- GRACE PERIOD (14-30 days) rather than immediate execution, for three reasons:
--   1. Regret — a meaningful share log back in to cancel
--   2. In-flight obligations — someone with a confirmed session tomorrow
--      shouldn't vanish and leave the mentor with a ghost booking
--   3. Batch execution — anonymization touches many tables; a scheduled job is
--      easier to make correct and idempotent than a request handler
--
-- 'anonymize' is what "delete completely" should mean. True DELETE would corrupt
-- every mentor's completion count and rating, and destroy financial records
-- you're required to keep once payments exist. Anonymization satisfies GDPR
-- erasure — the person is no longer identifiable:
--
--   users.email       -> deleted-{uuid}@invalid
--   users.first/last  -> 'Deleted' / 'User'
--   users.phone_e164  -> NULL
--   user_profiles     -> NULL avatar, banner, about_me, socials
--   auth_identities   -> HARD DELETE
--   auth_codes        -> HARD DELETE
--   calendar_connections -> HARD DELETE (revoke upstream first)
--   messages.body     -> '[deleted]' (only if the user requests it)
--   sessions, reviews, credit_transactions -> RETAINED, now anonymous
-- -----------------------------------------------------------------------------
CREATE TABLE account_deletion_requests (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  deletion_type  deletion_type NOT NULL,
  reason         text,
  requested_at   timestamptz NOT NULL DEFAULT now(),
  scheduled_for  timestamptz NOT NULL,
  executed_at    timestamptz,
  cancelled_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_deletion_requests_pending ON account_deletion_requests (user_id)
  WHERE executed_at IS NULL AND cancelled_at IS NULL;

SELECT attach_updated_at_triggers();

COMMIT;
