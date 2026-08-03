-- =============================================================================
-- 02_PROFILES
-- Mentor profiles, education, credentials, mentee goals.
-- Depends on 01_identity.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- mentor_profiles — replaces the non-derived parts of `Mentor (front search)`.
--
-- user_id IS THE PK. mentor_profile_id and user_id are therefore the same value
-- everywhere, which removes the ambiguity entirely. Mentor-only tables reference
-- mentor_profiles(user_id) rather than users(id) — same value, but the FK makes
-- it structurally impossible to attach a mentor-only row to a mentee.
--
-- WHAT'S NOT HERE (and why):
--   nameFirstLast, pictureProfile, Gender, countryOrigin  -> users/user_profiles
--   mentorLanguages                                        -> user_languages
--   mentorMentorshipSupport, mentorServices                -> mentor_service_offerings
--   degreeCategory, latestUniversity, Education            -> education_entries
--   Scholarship Experience                                 -> user_scholarship_experience
--   countCompletedSession, countReviewReceived,
--   percentageOfCompletedSession                           -> DERIVED at query time
--   meetingDuration                                        -> session_type_booking_configs
--   unavailableDateRange, unavailableDuration              -> availability_exceptions
--
-- LISTING STATUS — collapsed from two booleans to one enum.
-- An earlier draft had is_available + is_listed. Two problems with that:
--   1. is_available was DERIVABLE from availability_rules + exceptions +
--      existing sessions. Storing it recreates the exact drift that made the
--      front-search table untrustworthy.
--   2. "paused but visible" conflated two different things — searchability
--      (a profile attribute) and profile-page access (a rule about the VIEWER).
--
-- Profile page access is therefore NOT a column. It's a rule:
--   render IF listing_status = 'listed'
--          OR viewer has a session with this mentor (any status)
--          OR viewer is admin
-- A mentee with a completed session needs the profile to render regardless of
-- listing state, or their session history breaks and past reviews 404.
--
-- unlisted_reason is INTERNAL — drives admin dashboards and re-engagement,
-- never shown in search.
-- -----------------------------------------------------------------------------
CREATE TABLE mentor_profiles (
  user_id                     uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

  approval_status             approval_status NOT NULL DEFAULT 'pending',
  approved_at                 timestamptz,
  approved_by                 uuid REFERENCES users(id),
  decline_reason              text,

  listing_status              listing_status NOT NULL DEFAULT 'unlisted',
  unlisted_reason             unlisted_reason DEFAULT 'never_approved',
  unlisted_at                 timestamptz,

  headline                    text,
  years_of_experience         int,

  requires_booking_confirmation boolean NOT NULL DEFAULT true,

  -- Venue cascades: session type overrides, else this. Resolution is
  -- COALESCE(session_type.meeting_venue, mentor.default_meeting_venue).
  default_meeting_venue       meeting_provider NOT NULL DEFAULT 'google_meet',

  -- ONLY used when default_meeting_venue = 'custom'.
  -- A static personal room link (meet.google.com/abc-defg) means back-to-back
  -- sessions share a room and an early joiner walks into the previous session.
  -- That's a privacy incident, not a UX annoyance. Per-session links are
  -- generated at confirmation and stored on sessions.meeting_url.
  custom_meeting_url          text,

  -- Display/filter convenience. Full history lives on education_entries —
  -- a mentor with degrees from Nigeria, then the UK, then Canada was previously
  -- flattened to one value, which is exactly what "who studied in Canada"
  -- needs to search across.
  primary_study_country_code  char(2) REFERENCES countries(code),
  primary_study_program       text,

  created_at                  timestamptz NOT NULL DEFAULT now(),
  updated_at                  timestamptz NOT NULL DEFAULT now(),
  deleted_at                  timestamptz,
  legacy_bubble_id            text UNIQUE,

  CONSTRAINT custom_url_requires_custom_venue
    CHECK (custom_meeting_url IS NULL OR default_meeting_venue = 'custom')
);

-- These four indexes plus the user_profiles ones ARE the search implementation.
-- No Typesense, no Meilisearch, no synced denormalized table.
CREATE INDEX idx_mentor_searchable ON mentor_profiles (approval_status, listing_status)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_mentor_study_country ON mentor_profiles (primary_study_country_code)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_mentor_unlisted ON mentor_profiles (unlisted_reason, unlisted_at)
  WHERE listing_status = 'unlisted';


-- -----------------------------------------------------------------------------
-- mentor_service_offerings — what a mentor helps with.
-- Points at the SAME service_offerings table as mentee_goal_needs, which is what
-- makes basic matching a join (see docs/DECISIONS.md #12).
-- -----------------------------------------------------------------------------
CREATE TABLE mentor_service_offerings (
  mentor_user_id      uuid NOT NULL REFERENCES mentor_profiles(user_id) ON DELETE CASCADE,
  service_offering_id uuid NOT NULL REFERENCES service_offerings(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (mentor_user_id, service_offering_id)
);

CREATE INDEX idx_mentor_services_offering ON mentor_service_offerings (service_offering_id);


-- -----------------------------------------------------------------------------
-- user_scholarship_experience — USER-LEVEL, not mentor-level.
--
-- CORRECTION from an earlier draft that put this under mentor services. Two
-- different things were conflated:
--   A CREDENTIAL   — "I won a Chevening scholarship." A fact about a person.
--                    Any user can have it, including a mentee who won something
--                    and is now seeking a second degree.
--   A CAPABILITY   — "I can advise on Commonwealth applications." Mentor-side.
--
-- Correlated but not identical: someone can advise on scholarships they never
-- won, and a mentee can hold awards without offering anything.
--
-- `relationship` is what makes one table serve both. Mentor search filters on
-- relationship='advised'; a mentee profile displays relationship='awarded'.
-- -----------------------------------------------------------------------------
CREATE TABLE user_scholarship_experience (
  user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scholarship_program_id  uuid NOT NULL REFERENCES scholarship_programs(id),
  relationship            scholarship_relationship NOT NULL,
  year                    int,
  notes                   text,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, scholarship_program_id, relationship),
  CONSTRAINT year_sane CHECK (year IS NULL OR year BETWEEN 1950 AND 2100)
);

CREATE INDEX idx_scholarship_exp_program ON user_scholarship_experience
  (scholarship_program_id, relationship);


-- -----------------------------------------------------------------------------
-- user_awards — legacy Scholarship-Awards (17 rows), moved to user level.
-- Mentees can hold awards from day one.
--
-- VERIFICATION DECISION (this phase): OPTION A — don't verify, label clearly.
-- verification_status defaults to 'unverified' and NOTHING renders a checkmark.
-- Every verified claim is manual admin work and the queue never empties.
--
-- The columns exist now so switching on verify-on-request later is a feature
-- flag, not a migration.
--
-- UI NOTE THAT MATTERS MORE THAN THE SCHEMA: label at the field level
-- ("Awards — self-reported"), not in a footer disclaimer. A checkmark icon next
-- to "Chevening Scholar" reads as endorsement even with a tooltip saying
-- otherwise — a real liability question once money is involved.
-- -----------------------------------------------------------------------------
CREATE TABLE user_awards (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  institution         text NOT NULL,
  title               text NOT NULL,
  year                int,
  evidence_url        text,
  verification_status verification_status NOT NULL DEFAULT 'unverified',
  verified_at         timestamptz,
  verified_by         uuid REFERENCES users(id),
  rejection_reason    text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  legacy_bubble_id    text UNIQUE,
  CONSTRAINT award_year_sane
    CHECK (year IS NULL OR year BETWEEN 1950 AND EXTRACT(YEAR FROM now())::int + 1)
);

CREATE INDEX idx_user_awards_user ON user_awards (user_id) WHERE deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- education_entries — legacy Education (940 rows).
--
-- institution_id is NULLABLE and school_name_raw is ALWAYS kept. Unmatched
-- entries still display what the user typed and can be linked opportunistically
-- when they next edit their profile.
--
-- COUNTRY IS NEVER ASKED. It derives from institutions.country_code, resolved
-- once at write time. Storing the raw string instead would mean re-querying the
-- Hipolabs API on every profile render.
--
-- MIGRATION NOTE: Hipolabs autocomplete is already in use in Bubble, so school
-- names may already be clean. Check first:
--   SELECT school_name_raw, count(*) FROM staging.education GROUP BY 1 ORDER BY 2 DESC;
-- 200-400 distinct across 940 rows -> near-straight lookup, an afternoon.
-- 600+ with obvious variants -> free-typing got through, needs a fuzzy pass:
--   WHERE similarity(school_name_raw, name) < 0.8  -- auto-link above ~0.85
-- Capture `domain` going forward regardless: it's the only stable natural key.
-- -----------------------------------------------------------------------------
CREATE TABLE education_entries (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  institution_id    uuid REFERENCES institutions(id),
  school_name_raw   text NOT NULL,              -- always what the user typed
  school_short_form text,                       -- mostly folds into alt_names

  degree_level_id   uuid REFERENCES degree_levels(id),
  degree_category   text,                       -- legacy free text, migrate then deprecate
  study_course      text,
  study_program     text,
  field_of_interest text,                       -- -> tag FK when matching is built

  date_start        date,
  date_end          date,
  is_most_recent    boolean NOT NULL DEFAULT false,

  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz,
  legacy_bubble_id  text UNIQUE,

  CONSTRAINT education_dates_ordered
    CHECK (date_end IS NULL OR date_start IS NULL OR date_end >= date_start)
);

CREATE INDEX idx_education_user ON education_entries (user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_education_institution ON education_entries (institution_id);

-- Enforces one-most-recent-per-user at the DATABASE level.
-- `latestUniversity` on the old front-search table derives from this.
CREATE UNIQUE INDEX idx_education_one_most_recent ON education_entries (user_id)
  WHERE is_most_recent AND deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- mentee_goals — legacy Members Goals (720 rows). 1:1 with user.
--
-- completedSession is DROPPED — derived from sessions.
-- -----------------------------------------------------------------------------
CREATE TABLE mentee_goals (
  user_id           uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  degree_goal_id    uuid REFERENCES degree_levels(id),
  degree_goal_raw   text,                       -- unmappable legacy values land here
  target_start_term text,
  notes             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id  text UNIQUE
);


-- -----------------------------------------------------------------------------
-- mentee_goal_countries — legacy `Country Goal` list.
-- priority lets first-choice matches weight higher in ranking.
-- -----------------------------------------------------------------------------
CREATE TABLE mentee_goal_countries (
  user_id      uuid NOT NULL REFERENCES mentee_goals(user_id) ON DELETE CASCADE,
  country_code char(2) NOT NULL REFERENCES countries(code),
  priority     int NOT NULL DEFAULT 1,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, country_code)
);

CREATE INDEX idx_mentee_goal_countries_country ON mentee_goal_countries (country_code);


-- -----------------------------------------------------------------------------
-- mentee_goal_needs — legacy `Mentorship Goals` list.
--
-- SAME service_offerings table as mentor_service_offerings. This is the whole
-- point: Bubble had two separate option sets with no mapping, so "does this
-- mentor do what this mentee needs" required a hand-maintained mapping that
-- never existed. Now it's:
--
--   SELECT m.mentor_user_id, COUNT(*) AS overlap
--   FROM mentee_goal_needs g
--   JOIN mentor_service_offerings m USING (service_offering_id)
--   WHERE g.user_id = :mentee
--   GROUP BY 1 ORDER BY overlap DESC;
--
-- That works before any AI is involved.
-- -----------------------------------------------------------------------------
CREATE TABLE mentee_goal_needs (
  user_id             uuid NOT NULL REFERENCES mentee_goals(user_id) ON DELETE CASCADE,
  service_offering_id uuid NOT NULL REFERENCES service_offerings(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, service_offering_id)
);

CREATE INDEX idx_mentee_goal_needs_offering ON mentee_goal_needs (service_offering_id);

SELECT attach_updated_at_triggers();

COMMIT;
