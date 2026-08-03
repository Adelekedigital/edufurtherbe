-- =============================================================================
-- 04_SESSIONS
-- The core domain. Merges SessionBooking (1,073) + SessionTracker (935).
--
-- LARGEST AND MOST COMPLEX PHASE. Run the reconciliation BEFORE writing the
-- transform, not after:
--   - is the ~138-row gap entirely cancelled bookings with no tracker?
--   - did any booking produce MULTIPLE trackers (reschedules)?
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- session_types — planned `Dedicated_session_types`.
--
-- MIGRATION NOTE: every existing mentor gets an auto-created "General
-- Mentorship" session type during M4, so all 1,073 legacy sessions can carry a
-- session_type_id. That lets the column become NOT NULL after migration and
-- means exactly one code path, rather than a platform-default fallback forever.
-- -----------------------------------------------------------------------------
CREATE TABLE session_types (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentor_user_id    uuid NOT NULL REFERENCES mentor_profiles(user_id) ON DELETE CASCADE,

  name              text NOT NULL,
  description       text,
  category          text,
  application_stage text,

  is_active         boolean NOT NULL DEFAULT true,

  created_by        uuid REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz
);

CREATE INDEX idx_session_types_mentor ON session_types (mentor_user_id)
  WHERE is_active AND deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- session_type_booking_configs — 1:1 with session type.
--
-- duration_minutes IS THE SINGLE SOURCE OF TRUTH FOR DURATION.
-- CalendarSettings.meetingDuration-TxT and mentor_profiles duration are both
-- dropped. Availability = WHEN, session type = HOW LONG.
--
-- meeting_venue is NULLABLE = inherit from mentor_profiles.default_meeting_venue.
-- Null-means-inherit is unambiguous and this override earns its keep: a mentor
-- runs everything on Google Meet but does mock interviews on Zoom for recording.
-- -----------------------------------------------------------------------------
CREATE TABLE session_type_booking_configs (
  session_type_id     uuid PRIMARY KEY REFERENCES session_types(id) ON DELETE CASCADE,
  duration_minutes    int NOT NULL CHECK (duration_minutes BETWEEN 5 AND 480),
  min_notice_minutes  int NOT NULL DEFAULT 120,
  meeting_venue       meeting_provider,          -- NULL = inherit from mentor
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- session_type_questions — intake form definition.
-- Your planned table had BOTH `Order` and `Display_order`; one is dropped.
-- -----------------------------------------------------------------------------
CREATE TABLE session_type_questions (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_type_id  uuid NOT NULL REFERENCES session_types(id) ON DELETE CASCADE,
  question_text    text NOT NULL,
  question_type    question_type NOT NULL,
  is_required      boolean NOT NULL DEFAULT false,
  display_order    int NOT NULL DEFAULT 0,
  created_by       uuid REFERENCES users(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz
);

CREATE INDEX idx_questions_type ON session_type_questions (session_type_id, display_order)
  WHERE deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- session_type_question_options
-- Your planned `Question_multi-choice` was a column; options need their own rows.
-- -----------------------------------------------------------------------------
CREATE TABLE session_type_question_options (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  question_id  uuid NOT NULL REFERENCES session_type_questions(id) ON DELETE CASCADE,
  option_text  text NOT NULL,
  sort_order   int NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_question_options ON session_type_question_options (question_id, sort_order);


-- -----------------------------------------------------------------------------
-- sessions — SessionBooking + SessionTracker, merged.
--
-- WHY mentor_id AND mentee_id STAY ON THIS TABLE
-- ----------------------------------------------
-- The instinct to move them into session_participants and derive everything is
-- good normalization instinct. Two things block it:
--
--   1. THE EXCLUSION CONSTRAINT NEEDS mentor_id ON THE ROW.
--      EXCLUDE operates on columns of a single table; it cannot reference a
--      joined table. Move mentor_id to participants and you lose database-level
--      double-booking prevention — the exact bug class Bubble couldn't prevent
--      and that this migration exists to escape. The alternative is a BEFORE
--      INSERT trigger doing an overlap query, which is race-prone without
--      additional locking and is application logic pretending to be a constraint.
--
--   2. RLS GETS EXPENSIVE.
--        with columns:  USING (mentee_id = auth.uid() OR mentor_id = auth.uid())
--        participants:  USING (EXISTS (SELECT 1 FROM session_participants
--                                      WHERE session_id = sessions.id
--                                        AND user_id = auth.uid()))
--      The second runs per row on every query touching the most-read table.
--
-- These aren't denormalization — they EXPRESS A DOMAIN INVARIANT: sessions are
-- 1:1 between exactly one mentor and one mentee, by design. If that invariant
-- changes (cohort sessions, group workshops), move to a host+attendees model:
-- keep mentor_id as the host, drop mentee_id, use participants for attendees.
--
-- STATUS IS NOT DERIVED FROM ATTENDANCE.
-- --------------------------------------
-- Status is a lifecycle state that exists BEFORE anyone attends. A session is
-- 'pending_mentor_approval' at creation and 'cancelled' if called off — in both
-- cases there is no attendance data to derive from. The relationship is
-- one-directional:
--   attendance INFORMS:      confirmed -> completed | no_show
--   attendance DOES NOT DEFINE: pending | confirmed | cancelled | declined | expired
-- A job runs after starts_at + duration, reads participant attendance, and
-- writes ONE session_events transition.
--
-- DROPPED AS DERIVED: datePicked, datePickedText, slotBookedTime, session time,
-- Weekday(number) — all recoverable from starts_at.
-- DROPPED AS DUPLICATE: SessionID, TrackID, Mentor(this session),
-- Mentee(userdatatype), Mentor(userdatatype), Canceled.
-- -----------------------------------------------------------------------------
CREATE TABLE sessions (
  id                    uuid PRIMARY KEY DEFAULT uuid_generate_v7(),

  mentor_id             uuid NOT NULL REFERENCES users(id),
  mentee_id             uuid NOT NULL REFERENCES users(id),
  created_by            uuid REFERENCES users(id),   -- who CLICKED book; diverges
                                                     -- when a mentor or admin
                                                     -- books on someone's behalf
  session_type_id       uuid REFERENCES session_types(id),  -- NOT NULL after M4

  status                session_status NOT NULL DEFAULT 'pending_mentor_approval',

  starts_at             timestamptz NOT NULL,
  duration_minutes      int NOT NULL CHECK (duration_minutes BETWEEN 5 AND 480),

  topic                 text,
  booking_message       text,

  meeting_provider      meeting_provider,
  meeting_url           text,                    -- generated per session
  external_room_id      text,                    -- google/dailyRoomName
  external_calendar_event_id text,               -- googleCalEventId

  rescheduled_from_session_id uuid REFERENCES sessions(id),

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id      text UNIQUE,

  CONSTRAINT no_self_booking CHECK (mentor_id <> mentee_id)
);

-- THE CONSTRAINT BUBBLE COULD NEVER ENFORCE.
-- Requires btree_gist (see 00_foundation).
ALTER TABLE sessions ADD CONSTRAINT no_mentor_double_booking
  EXCLUDE USING gist (
    mentor_id WITH =,
    tstzrange(starts_at, starts_at + (duration_minutes || ' minutes')::interval) WITH &&
  ) WHERE (status IN ('pending_mentor_approval', 'confirmed'));

CREATE INDEX idx_sessions_mentor_upcoming ON sessions (mentor_id, starts_at)
  WHERE status IN ('pending_mentor_approval', 'confirmed');
CREATE INDEX idx_sessions_mentee_upcoming ON sessions (mentee_id, starts_at)
  WHERE status IN ('pending_mentor_approval', 'confirmed');
CREATE INDEX idx_sessions_mentor_completed ON sessions (mentor_id)
  WHERE status = 'completed';
CREATE INDEX idx_sessions_starts_at ON sessions (starts_at);


-- -----------------------------------------------------------------------------
-- session_participants — ATTENDANCE. One row per person per session.
--
-- Replaces four parallel legacy columns: Last Joined(mentee), Last Joined(Mentor),
-- TrackStatus(mentee), TrackStatus(Mentor).
--
-- Rows are created in the SAME TRANSACTION as the session insert, so they can
-- never disagree with mentor_id/mentee_id.
-- -----------------------------------------------------------------------------
CREATE TABLE session_participants (
  session_id        uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  user_id           uuid NOT NULL REFERENCES users(id),
  role              session_role NOT NULL,
  joined_at         timestamptz,
  left_at           timestamptz,
  attendance_status attendance_status NOT NULL DEFAULT 'pending',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, user_id)
);

-- Catches any drift between sessions.mentor_id and the participant rows.
CREATE UNIQUE INDEX idx_one_mentor_per_session ON session_participants (session_id)
  WHERE role = 'mentor';
CREATE INDEX idx_session_participants_user ON session_participants (user_id);


-- -----------------------------------------------------------------------------
-- session_events — IMMUTABLE lifecycle log.
--
-- Replaces the scattered flags: bookingRequestAccepted, SessionCancel(Y/N),
-- Canceled By, Session Cancel/Decline Message, Expiration, statusApproved-
-- DeclinedDate.
--
-- reason_code vs reason_text — TWO DIFFERENT FIELDS:
--   reason_text — free text from the human. "Sorry, conference clash."
--   reason_code — enum you GROUP BY. Drives automated policy.
--
-- Why the code matters: "what % of mentor-side cancellations are scheduling
-- conflicts" is a product question that decides whether you build reschedule
-- flows. Free text can't answer it without someone reading 200 rows. And refund
-- rules run off the code, not the prose: mentor_unavailable -> refund;
-- mentee_no_longer_needed within 24h of start -> don't.
--
-- Also: with events you can answer "how many sessions were cancelled by mentors
-- within 24h of start" — impossible today, since Canceled By has no timestamp.
--
-- actor_id is NULLABLE: null means the system did it (expiry job). A null actor
-- is more honest than inventing a system user.
--
-- APPEND-ONLY. Revoke UPDATE/DELETE from the application role.
-- -----------------------------------------------------------------------------
CREATE TABLE session_events (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  from_status session_status,                   -- null on the creation event
  to_status   session_status NOT NULL,
  actor_id    uuid REFERENCES users(id),        -- null = system
  actor_type  actor_type NOT NULL DEFAULT 'user',
  reason_code session_reason_code,
  reason_text text,
  metadata    jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_session_events_session ON session_events (session_id, created_at);
CREATE INDEX idx_session_events_reason ON session_events (reason_code, created_at)
  WHERE reason_code IS NOT NULL;


-- -----------------------------------------------------------------------------
-- session_notes — NEW. Absent from the Bubble dump entirely.
-- Mentors will want to record "applying to Toronto, December deadline, needs
-- help with SOP." visibility='shared' lets them send it to the mentee.
-- -----------------------------------------------------------------------------
CREATE TABLE session_notes (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  author_id   uuid NOT NULL REFERENCES users(id),
  body        text NOT NULL,
  visibility  text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'shared')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz
);

CREATE INDEX idx_session_notes_session ON session_notes (session_id)
  WHERE deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- intake_submissions — THE MISSING PARENT.
-- Your planned Intake_answers referenced Fk_intake_submission_id but no
-- submissions table existed in the dump. Answers hang off the submission; the
-- submission points at the session. Fk_mentee and Fk_session_booking_id are
-- dropped from the answers table — they belong here, once.
-- -----------------------------------------------------------------------------
CREATE TABLE intake_submissions (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  mentee_id     uuid NOT NULL REFERENCES users(id),
  status        intake_status NOT NULL DEFAULT 'draft',
  submitted_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id)
);


-- -----------------------------------------------------------------------------
-- intake_answers
-- CHECK constraints ensure a file_upload answer can't carry text and vice versa.
-- -----------------------------------------------------------------------------
CREATE TABLE intake_answers (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  submission_id     uuid NOT NULL REFERENCES intake_submissions(id) ON DELETE CASCADE,
  question_id       uuid NOT NULL REFERENCES session_type_questions(id),
  answer_text       text,
  file_storage_key  text,
  selected_option_id uuid REFERENCES session_type_question_options(id),
  answered_at       timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (submission_id, question_id),
  CONSTRAINT exactly_one_answer_form CHECK (
    (answer_text IS NOT NULL)::int
  + (file_storage_key IS NOT NULL)::int
  + (selected_option_id IS NOT NULL)::int = 1
  )
);

CREATE INDEX idx_intake_answers_submission ON intake_answers (submission_id);

SELECT attach_updated_at_triggers();

COMMIT;
