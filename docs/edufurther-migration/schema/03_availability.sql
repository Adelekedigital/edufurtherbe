-- =============================================================================
-- 03_AVAILABILITY
-- Mentor availability, exceptions, external calendar connections.
--
-- HIGHEST-RISK TRANSFORM IN THE MIGRATION. Legacy CalendarSettings stored
-- pre-formatted LOCAL TIME STRINGS (12hr-TXT, 24hr-TXT) alongside actual times.
-- Getting the timezone conversion wrong here silently produces wrong booking
-- slots, and DST transitions break it twice a year. Test against real mentors'
-- known availability before cutover.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- availability_rules — recurring weekly windows. Legacy CalendarSettings (192).
--
-- 12 legacy columns -> 6. Four of them were the same fact in different display
-- formats (12hr/24hr, start/end), which is a rendering concern that belongs in
-- the UI layer, not storage.
--
-- MULTIPLE ROWS PER DAY handles split availability — morning and afternoon with
-- a lunch gap — which the legacy one-row-per-day structure could not represent:
--
--   mentor  | day | start | end   | timezone
--   --------+-----+-------+-------+--------------
--   abc-123 |  1  | 09:00 | 12:00 | Africa/Lagos
--   abc-123 |  1  | 14:00 | 17:00 | Africa/Lagos
--
-- TIME STORAGE RULE:
--   wall-clock time + IANA zone  -> for RULES (recurring, DST-aware)
--   timestamptz                  -> for INSTANTS (a specific session)
-- Storing a pre-formatted local string for a recurring rule is what breaks
-- across DST.
--
-- meetingDuration-TxT is DROPPED. Availability defines WHEN a mentor is free;
-- session types define HOW LONG a session is. Two sources of truth for duration
-- is a bug waiting to happen. See session_type_booking_configs.
--
-- SLOTS ARE NEVER STORED. Computed at query time:
--   rules for that weekday
--     minus exceptions overlapping that date
--     minus existing confirmed sessions
--     sliced into session_type duration increments
--     filtered by min_notice_minutes
--     converted to the VIEWER's timezone for display
-- Cache with a 30s TTL only if it ever gets slow. It won't at 192 rules.
-- -----------------------------------------------------------------------------
CREATE TABLE availability_rules (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentor_user_id  uuid NOT NULL REFERENCES mentor_profiles(user_id) ON DELETE CASCADE,

  day_of_week     int  NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0 = Sunday
  start_time      time NOT NULL,
  end_time        time NOT NULL,
  timezone        text NOT NULL,                -- IANA, e.g. Africa/Lagos

  is_active       boolean NOT NULL DEFAULT true,

  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  legacy_bubble_id text UNIQUE,

  CONSTRAINT availability_window_ordered CHECK (end_time > start_time)
);

CREATE INDEX idx_availability_mentor ON availability_rules (mentor_user_id, day_of_week)
  WHERE is_active AND deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- availability_exceptions — legacy CalendarExtra (5 rows), plus the
-- unavailableDateRange / unavailableDuration fields from the front-search table.
--
-- Two kinds:
--   'block'    — mentor is NOT available during this range (holiday, exam period)
--   'override' — mentor IS available outside their normal rules (one-off slot)
--
-- daterange with GIST indexing makes overlap queries fast and expressive.
-- -----------------------------------------------------------------------------
CREATE TABLE availability_exceptions (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  mentor_user_id      uuid NOT NULL REFERENCES mentor_profiles(user_id) ON DELETE CASCADE,

  type                availability_exception_type NOT NULL,
  date_range          daterange NOT NULL,
  start_time          time,                     -- null = whole day
  end_time            time,
  timezone            text NOT NULL,
  reason              text,

  max_sessions_per_day int,                     -- legacy meetingDailySessions

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id    text UNIQUE,

  CONSTRAINT exception_times_paired
    CHECK ((start_time IS NULL) = (end_time IS NULL)),
  CONSTRAINT exception_window_ordered
    CHECK (start_time IS NULL OR end_time > start_time)
);

CREATE INDEX idx_availability_exceptions_range
  ON availability_exceptions USING gist (mentor_user_id, date_range);


-- -----------------------------------------------------------------------------
-- calendar_connections — external calendar OAuth.
--
-- LEGACY ARCHAEOLOGY: eight cal* columns sat on the User table. Most were
-- Cal.com's API vocabulary (calDefaultScheduleId, calEventId, calClientId) —
-- Google Calendar has no concept of a "default schedule ID." Those are dead
-- weight from an abandoned integration and are NOT migrated.
--
-- NO TOKENS STORED. That's the point of Composio: it holds the OAuth
-- credentials, we hold a reference. Removes the token-encryption requirement
-- and a meaningful chunk of security surface.
--
-- KNOWN CONSTRAINT: on Composio's self-serve plans, credentials pass through
-- Composio's cloud — even with your own OAuth app, their backend callback URL
-- is what gets registered with the provider. Self-hosting is Enterprise-only.
-- So users' calendar tokens live in a third party's infrastructure. Accepted
-- for this phase; see docs/DEFERRED.md for the Nango / direct-API exit path.
--
-- GOOGLE VERIFICATION: calendar.events and calendar.readonly are SENSITIVE
-- scopes -> app + brand verification, no CASA security assessment. Weeks, mostly
-- paperwork. (Gmail/Drive read are RESTRICTED -> CASA, months, thousands of
-- dollars. Not needed here.) No integration platform bypasses this if you want
-- your own brand on the consent screen. START THIS EARLY — it's calendar-time,
-- not eng-time, and it's the long pole.
-- -----------------------------------------------------------------------------
CREATE TABLE calendar_connections (
  id                   uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  provider             calendar_provider NOT NULL,
  composio_auth_id     text,                    -- legacy composioAuthId
  external_account_id  text,

  status               connection_status NOT NULL DEFAULT 'active',
  last_synced_at       timestamptz,
  last_error           text,

  connected_at         timestamptz NOT NULL DEFAULT now(),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id     text UNIQUE
);

CREATE UNIQUE INDEX idx_calendar_connections_active
  ON calendar_connections (user_id, provider) WHERE status = 'active';

SELECT attach_updated_at_triggers();

COMMIT;
