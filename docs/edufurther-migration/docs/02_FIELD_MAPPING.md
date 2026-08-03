# Field Mapping: Legacy Bubble → New Schema

Every table and field from the original dump, in the order it was listed.

**Legend**
- **DROP** — not migrated (redundant, replaced, or display-only)
- **DERIVED** — no longer stored, computed at query time
- **NEW** — no legacy equivalent

---

## Summary

| Legacy table | Rows | Becomes | Phase |
|---|---|---|---|
| User | 1,200 | `users`, `user_profiles`, `auth_identities`, `auth_codes`, `user_onboarding`, `user_legal_consents`, `admin_users`, `calendar_connections`, `user_languages` | M1 |
| SessionBooking (member) | 1,073 | `sessions` (merged) | M4 |
| SessionTracker | 935 | `sessions`, `session_participants`, `session_events` | M4 |
| Members Goals | 720 | `mentee_goals`, `mentee_goal_countries`, `mentee_goal_needs` | M2 |
| Mentor Services | 31 | `service_offerings`, `mentor_service_offerings` | M2 |
| Reviews | 53 | `reviews` | M5 |
| Scholarship-Awards | 17 | `user_awards` | M2 |
| PersonalInfo | 858 | `user_profiles`, `user_languages` | M1 |
| CalendarSettings | 192 | `availability_rules` | M3 |
| Education | 940 | `education_entries`, `institutions` | M2 |
| Mentor (front search) | 44 | `mentor_profiles` + mostly **DERIVED** | M2 |
| Notifications | 681 | `notifications`, `notification_recipients` | M6 |
| CalendarExtra | 5 | `availability_exceptions` | M3 |
| VB-Vision Boards | 10 | `vision_boards`, `vision_board_milestones` (rebuild) | B5 |
| messageStarters | 13 | `conversations`, `conversation_participants` | M6 |
| messageThreads | 44 | `messages` | M6 |

---

## 1. User (1,200)

### → `users`

| Legacy | New | Note |
|---|---|---|
| Email | `email` | citext; partial unique index where `deleted_at IS NULL` |
| email verified date | `email_verified_at` | |
| First Name | `first_name` | |
| Last Name | `last_name` | |
| First and Last Name | — | **DROP** — DERIVED, format in API layer |
| Role | `primary_role` | **UX hint only.** Authorization = profile existence |
| UserTimezonID | `timezone` | IANA string |
| Last Active | `last_active_at` | |
| — | `phone_e164` | **NEW** — absent from entire dump; required for WhatsApp |
| — | `phone_verified_at`, `phone_country_code` | **NEW** |
| — | `password_hash` | Nullable; OTP is primary auth |
| — | `created_at`, `updated_at`, `deleted_at`, `legacy_bubble_id` | **NEW** convention |

### → `auth_identities`

| Legacy | New |
|---|---|
| Registration format | `provider` (`google` \| `linkedin`) |
| — | `provider_user_id`, `linked_at`, `last_used_at` |

One row per linked provider. Fixes the single-option-set limitation.

### → `auth_codes`

| Legacy | New |
|---|---|
| password reset confirm | `code_hash` (**hashed**, never plaintext) |
| New password change date | `consumed_at` |
| — | `purpose`, `channel`, `destination`, `expires_at`, `attempt_count`, `max_attempts`, `invalidated_at`, `requested_ip` |

### → `user_onboarding`

| Legacy | New |
|---|---|
| User-last-onboarding-step | `last_step` |
| registration completed | `completed_at` |
| Registration completed (Y/N) | **DROP** — duplicate |

### → `user_legal_consents`

| Legacy | New |
|---|---|
| Terms agreed date | `consented_at` + `legal_document_id` |

Records *what* was accepted, not just when.

### → `admin_users`

| Legacy | New |
|---|---|
| Admin (option set) | `admin_role` |
| — | `granted_by`, `granted_at`, `revoked_at`, `revoked_by` |

### → `calendar_connections`

| Legacy | New | Note |
|---|---|---|
| composioAuthId | `composio_auth_id` | |
| calAccessToken | **DROP** | Composio holds tokens |
| calAccessTokenExpiresAt | **DROP** | |
| calRefreshToken | **DROP** | |
| calRefreshTokenExpiresAt | **DROP** | |
| calClientId | **DROP** | Cal.com residue |
| calDefaultScheduleId | **DROP** | Cal.com residue — Google has no such concept |
| calEventId | → `sessions.external_calendar_event_id` | |
| — | `provider`, `status`, `last_synced_at`, `last_error` | **NEW** |

### Remaining User fields

| Legacy | Destination |
|---|---|
| bookingCredit | → `credit_lots` |
| bookingCreditRenewDate | → `credit_lots.expires_at` |
| member goal | → `mentee_goals.user_id` (FK direction flips) |
| Mentor | → `mentor_profiles.user_id` (FK direction flips) |
| mentor service | → `mentor_service_offerings` junction |
| Personal Info | → `user_profiles.user_id` (FK direction flips) |
| Education | → `education_entries.user_id` (FK direction flips) |
| Interest | **DEFERRED** → `tags` / `user_tags` |
| emailitContact_id | → `user_profiles.email_provider_contact_id` |
| User Profile image | → `user_profiles.avatar_url` (**file must be re-hosted**) |

> **Note the FK direction flip.** Bubble stored lists on the parent; Postgres puts
> the FK on the child. `User.Education` (list) → `education_entries.user_id`.

---

## 2 + 3. SessionBooking (1,073) + SessionTracker (935) → merged

**Before writing the transform:** confirm the ~138-row gap is cancelled bookings
with no tracker, and check whether any booking produced multiple trackers.

### → `sessions`

| Legacy | From | New |
|---|---|---|
| Session Initiator | Booking | `mentee_id` |
| Mentor | Booking | `mentor_id` |
| Creator | Booking | `created_by` |
| Session topic | Booking | `topic` |
| Session booking Message | Booking | `booking_message` |
| Duration / Session Duration | Both | `duration_minutes` |
| SessionDateTime-UTC | Booking | `starts_at` (timestamptz) |
| sessionStatus | Booking | `status` |
| Meeting venue / meetingVenue | Both | `meeting_provider` |
| google/dailyMeetingVenue | Both | `meeting_provider` |
| google/dailyRoomName | Both | `external_room_id` |
| Meetinglink | Tracker | `meeting_url` |
| googleCalEventId | Booking | `external_calendar_event_id` |
| datePicked | Booking | **DROP** — DERIVED from `starts_at` |
| datePickedText | Booking | **DROP** — DERIVED, format in API |
| slotBookedTime | Booking | **DROP** — DERIVED |
| session time | Tracker | **DROP** — DERIVED |
| Weekday (number) | Booking | **DROP** — DERIVED |
| SessionID | Tracker | **DROP** — tables merged |
| TrackID | Tracker | **DROP** — `sessions.id` |
| Mentor(this session) | Tracker | **DROP** — duplicate |
| Mentee(userdatatype) | Tracker | **DROP** — duplicate |
| Mentor(userdatatype) | Tracker | **DROP** — duplicate |
| — | | `session_type_id` **NEW**, nullable during migration |
| — | | `rescheduled_from_session_id` **NEW** |

### → `session_participants`

| Legacy | From | New |
|---|---|---|
| Last Joined(mentee) | Tracker | `joined_at` where `role = 'mentee'` |
| Last Joined(Mentor) | Tracker | `joined_at` where `role = 'mentor'` |
| TrackStatus(mentee) | Tracker | `attendance_status` |
| TrackStatus(Mentor) | Tracker | `attendance_status` |

Two rows per session replacing four parallel columns.

### → `session_events`

| Legacy | From | Becomes |
|---|---|---|
| bookingRequestAccepted | Booking | event `pending_mentor_approval → confirmed` |
| SessionCancel (Y/N) | Booking | **DERIVED** from `status = 'cancelled'` |
| Canceled | Tracker | **DROP** — duplicate |
| Canceled By | Booking | `actor_id` on the cancel event |
| Session Cancel/Decline Message | Booking | `reason_text` |
| Expiration | Tracker | event `→ expired`, written by scheduled job |
| — | | `reason_code` **NEW** — enum, queryable, drives refund policy |

### → `outbox_events`

| Legacy | From |
|---|---|
| trackedSessionPosthog (Y/N) | Booking |
| sessionTrackedPosthog | Tracker |

Both **DROP** from domain tables.

---

## 4. Members Goals (720)

| Legacy | New |
|---|---|
| degreeGoal (text) | `mentee_goals.degree_goal_id` FK → `degree_levels` |
| — | `mentee_goals.degree_goal_raw` (unmappable legacy values) |
| Country Goal (list) | `mentee_goal_countries` junction + `priority` |
| Mentorship Goals (list) | `mentee_goal_needs` junction → **shared** `service_offerings` |
| completedSession | **DERIVED** — `COUNT(*)` from `sessions` |

Expect a messy mapping pass on `degreeGoal`: "Masters", "masters", "MSc",
"Master's Degree" all coexist across 720 rows.

---

## 5. Mentor Services (31)

| Legacy | New |
|---|---|
| Mentor Services/Support (list) | `service_offerings` lookup + `mentor_service_offerings` junction |
| Scholarship Experience (list) | **MOVED to user level** → `user_scholarship_experience` with `relationship = 'advised'` |

Also absorbs `Mentor (front search).mentorMentorshipSupport` and `.mentorServices`
— currently the same data stored twice.

---

## 6. Reviews (53)

| Legacy | New | Note |
|---|---|---|
| communicationRating | `communication_rating` | `CHECK BETWEEN 1 AND 5` |
| knowledgeRating | `knowledge_rating` | CHECK |
| practicalityRating | `practicality_rating` | CHECK |
| supportRating | `support_rating` | CHECK |
| likertValuableRating | `valuable_rating` | CHECK |
| npsRecommendScore | `nps_recommend_score` | `CHECK BETWEEN 0 AND 10` |
| privateReview | `private_review` | |
| publicReview | `public_review` | |
| reviewedBy | `reviewed_by` | |
| reviewedFor | `reviewed_for` | |
| — | `session_id` | **NEW** — which session produced this |
| — | `reviewed_role` | **NEW** — dual roles shouldn't blend reputations |
| — | | `UNIQUE (session_id, reviewed_by)`, `CHECK (reviewed_by <> reviewed_for)` |

---

## 7. Scholarship-Awards (17) → `user_awards`

| Legacy | New |
|---|---|
| Award-institution | `institution` |
| Award-title | `title` |
| Award-year | `year` — `CHECK BETWEEN 1950 AND current_year + 1` |
| — | `user_id` (**user-level, not mentor-level**) |
| — | `verification_status` **NEW** — defaults `unverified`, nothing renders a checkmark |
| — | `evidence_url`, `verified_at`, `verified_by` **NEW**, unused this phase |

---

## 8. PersonalInfo (858)

### → `user_profiles`

| Legacy | New | Note |
|---|---|---|
| About me | `about_me` | |
| Profile banner Image | `banner_url` | **file must be re-hosted** |
| Gender | `gender` | |
| Country of Origin | `origin_country_code` | ISO 3166-1 alpha-2 |
| OriginCountry (text) | **DROP** | duplicate |
| Country of study(mentor) | → `mentor_profiles.primary_study_country_code` | |
| StudyCountry (text) | **DROP** | duplicate |
| Social Linkedin | `social_linkedin` | |
| Social Twitter | `social_twitter` | |
| Social Youtube | `social_youtube` | |
| *(from User)* User Profile image | `avatar_url` | |
| *(from User)* emailitContact_id | `email_provider_contact_id` | |
| — | `current_country_code` | **NEW** — where they live now |

### → `user_languages`

| Legacy | New |
|---|---|
| Language | `user_languages` row |
| list-Language | `user_languages` rows |
| *(front search)* mentorLanguages | **DROP** — duplicate |

ISO 639-3 via the `languages` lookup. Attached to `users`, not `mentor_profiles`.

---

## 9. CalendarSettings (192) → `availability_rules`

| Legacy | New |
|---|---|
| dayOfWeekIn | `day_of_week` (0–6) |
| daysOfWeek-O/S | **DROP** — duplicate |
| availableDay-Bool | `is_active` |
| startTime | `start_time` (time) |
| endTime | `end_time` (time) |
| timeZone | `timezone` (IANA) |
| 12hr-localStartTime-TXT | **DROP** — DERIVED, format in UI |
| 12hr-localEndTime-TXT | **DROP** — DERIVED |
| 24hr-locatStartTime-TXT | **DROP** — DERIVED |
| 24hr-localEndTime-TXT | **DROP** — DERIVED |
| meetingDuration-TxT | **DROP** — session types own duration |
| meetingVenue | → `mentor_profiles.default_meeting_venue` |

Twelve columns → six. **Highest-risk transform in the migration.**

---

## 10. Education (940) → `education_entries` + `institutions`

| Legacy | New |
|---|---|
| schoolName | `school_name_raw` (always kept) + `institution_id` FK |
| shortForm | `school_short_form` (mostly folds into `institutions.alt_names`) |
| degreeCategory | `degree_category` (migrate, then deprecate → `degree_level_id`) |
| studyCourse | `study_course` |
| studyProgram-O/S | `study_program` |
| studyFieldInsterest | `field_of_interest` (→ tag FK when matching is built) |
| dateStart | `date_start` |
| dateEnd | `date_end` |
| mostRecentDegree | `is_most_recent` |
| — | `user_id` FK |
| — | **country DERIVES** from `institutions.country_code` — never asked |

```sql
CREATE UNIQUE INDEX ON education_entries (user_id)
  WHERE is_most_recent AND deleted_at IS NULL;
```

**Hipolabs is already in use in Bubble**, so names may already be clean. Check
before planning the transform:

```sql
SELECT school_name_raw, count(*) FROM staging.education GROUP BY 1 ORDER BY 2 DESC;
```

200–400 distinct across 940 rows → near-straight lookup, an afternoon.
600+ with obvious variants → fuzzy pass with `pg_trgm`, auto-link above ~0.85.

---

## 11. Mentor (front search) (44) → mostly deleted

### → `mentor_profiles` (the real parts)

| Legacy | New |
|---|---|
| approvedText | `approval_status` |
| statusApproved-DeclinedDate | `approved_at` |
| confirmationRequired | `requires_booking_confirmation` |
| availableStatus | `listing_status` |
| unavailableDateRange | → `availability_exceptions` |
| unavailableDuration | → `availability_exceptions` |
| meetingDuration | → `session_type_booking_configs.duration_minutes` |
| meetingVenueSelection | `default_meeting_venue` |
| meetingVenueLink | `custom_meeting_url` (only when venue = `custom`) |
| studyCountry | `primary_study_country_code` |
| studyCourse | → `education_entries` |
| studyProgram | `primary_study_program` |
| — | `approved_by`, `decline_reason`, `unlisted_reason`, `headline` **NEW** |

### DERIVED

| Legacy | Computed from |
|---|---|
| countCompletedSession | `COUNT(*) FROM sessions WHERE status = 'completed'` |
| countReviewReceived | `COUNT(*) FROM reviews` |
| percentageOfCompletedSession | ratio from `sessions` |

### DROP — duplicated from the real source

| Legacy | Real source |
|---|---|
| nameFirstLast | `users.first_name` + `last_name` |
| pictureProfile | `user_profiles.avatar_url` |
| Gender | `user_profiles.gender` |
| countryOrigin | `user_profiles.origin_country_code` |
| mentorLanguages | `user_languages` |
| mentorMentorshipSupport | `mentor_service_offerings` |
| mentorServices | `mentor_service_offerings` |
| degreeCategory | `education_entries` |
| latestUniversity | `education_entries WHERE is_most_recent` |
| Education (list) | `education_entries.user_id` |

### Replacement

No table. Indexes on `mentor_profiles`, `user_profiles`, `user_languages`, and
`education_entries` — see `02_profiles.sql`.

---

## 12. Notifications (681)

| Legacy | New |
|---|---|
| Notification Title | `notifications.title` |
| Notification Sender body | `notifications.body` |
| Notify Type | `notifications.type` |
| Session | `notifications.session_id` (typed FK) |
| Review | `notifications.review_id` (typed FK) |
| Receiver | `notification_recipients` row |
| Receiver (list of users) | `notification_recipients` rows |
| Seen (list of users) | `notification_recipients.read_at` |
| Seen receiver | **DROP** — duplicate |
| Seen sender | **DROP** — duplicate |
| — | `conversation_id` (typed FK) **NEW** |
| — | `context` jsonb **NEW** — everything else |

---

## 13. CalendarExtra (5) → `availability_exceptions`

| Legacy | New |
|---|---|
| block-Date(s) (list) | `date_range` (daterange) |
| block-dates | **DROP** — duplicate |
| calendarSettingList | **DROP** — FK flips to `mentor_user_id` |
| meetingDailySessions | `max_sessions_per_day` |
| meetingDuration | **DROP** — session types own duration |
| meetingVenue/Link | → `mentor_profiles` defaults |
| — | `type` (`block` \| `override`), `start_time`, `end_time`, `reason` |

---

## 14. VB-Vision Boards (10) → rebuild

### → `vision_boards`

| Legacy | New |
|---|---|
| visionStatement | `statement` |
| goalName | `name` |
| goalStatus | `status` |
| goalCompletedStatus | **DERIVED** from `completed_at` |
| dateTargetedCompletion | `target_completion_date` |
| Datecompleted | `completed_at` |
| datePaused | `paused_at` |
| pauseReason | `pause_reason` |
| dateResumed | `resumed_at` |
| numOfMonth | `duration_months` |
| visionBoardCardShareImg | `card_share_image_url` (**re-host**) |
| visionBoardCertShareImg | `cert_share_image_url` (**re-host**) |

### → `vision_board_milestones`

| Legacy fields | `milestone_type` |
|---|---|
| Country Selection - listOfCountry | `country_selection` |
| School Selection - Dream School, numOfSchools | `school_selection` |
| Program Selection - programType, targetFieldOfStudy | `program_selection` |
| Test prep - testType, targetScore | `test_prep` |
| Document prep - documentType | `document_prep` |
| Interview prep - interviewType | `interview_prep` |
| Scholarship - fundingType | `scholarship` |

### DERIVED from sessions

`numOfSessions`, `sessionCompletedCount`, `sessionMinutesCompleted`,
`totalNumberOfSessionToComplete`

---

## 15 + 16. messageStarters (13) + messageThreads (44)

### messageStarters → `conversations` + `conversation_participants`

| Legacy | New |
|---|---|
| messageRequestAccept | `conversations.status` |
| lastMessageContent | `conversations.last_message_preview` (**trigger-maintained**) |
| sendBy | `conversation_participants` row |
| receiveBy | `conversation_participants` row |

### messageThreads → `messages`

| Legacy | New |
|---|---|
| messageContent | `body` |
| messageStarter | `conversation_id` |
| sendBy | `sender_id` |
| receivedBy | **DROP** — DERIVED from participants |
| sentAt | `sent_at` |
| dateLastEdited | `edited_at` |
| seenRead | `conversation_participants.last_read_at` |
| softDelete | `deleted_at` |
| — | `message_type`, `payload` **NEW** — enables action cards later |

---

## 17. Planned tables (your dump) → refined

### `Dedicated_session_types` → `session_types`

| Yours | New |
|---|---|
| Fk_mentor | `mentor_user_id` |
| Session_name | `name` |
| Session_description | `description` |
| Session_category | `category` |
| Application_stage | `application_stage` |
| Is_active | `is_active` |

### `Session_type_booking_configs`

| Yours | New |
|---|---|
| Fk_dedicated_session_type_id | `session_type_id` (PK, 1:1) |
| Session_duration_minutes | `duration_minutes` — **single source of truth** |
| Min_booking_notice_minutes | `min_notice_minutes` |
| — | `meeting_venue` **NEW**, nullable = inherit from mentor |

### `Session_type_questions`

| Yours | New |
|---|---|
| Fk_dedicated_session_type_id | `session_type_id` |
| Question_text | `question_text` |
| Question_type | `question_type` (enum) |
| Question_required | `is_required` |
| Display_order | `display_order` |
| Order | **DROP** — duplicate |
| Fk-created_by | `created_by` |
| Question_multi-choice | **→ `session_type_question_options` child table** |

### `Intake_answers` → + missing parent

Your dump referenced `Fk_intake_submission_id` but no submissions table existed.

| Yours | New |
|---|---|
| — | **`intake_submissions`** **NEW** — id, session_id, mentee_id, status, submitted_at |
| Fk_intake_submission_id | `submission_id` |
| Fk_session_type_question_id | `question_id` |
| Response_answer_text | `answer_text` |
| Response_file_url | `file_storage_key` |
| Answered_at | `answered_at` |
| Fk_mentee | **DROP** — on the submission, not every answer |
| Fk_session_booking_id | **DROP** — on the submission |
| — | `selected_option_id` **NEW** + CHECK: exactly one answer form |

---

## 18. New tables (no legacy source)

| Table | Purpose |
|---|---|
| `credit_lots`, `credit_transactions` | Replaces `bookingCredit` + `bookingCreditRenewDate` |
| `referrals`, `referral_unlocks` | The credit gate |
| `booking_policies`, `booking_attempts` | Concurrency limits + rejection tracking |
| `user_infractions`, `user_standing`, `mentor_blocks`, `user_reports` | Penalties |
| `notification_outbox`, `push_subscriptions`, `whatsapp_templates`, `notification_channel_preferences` | Multi-channel delivery |
| `message_attachments` | File/media sharing |
| `session_notes` | Mentor private notes |
| `intake_submissions` | The missing parent |
| `institutions`, `scholarship_programs`, `degree_levels`, `service_offerings`, `languages`, `countries` | Lookups |
| `audit_log`, `outbox_events` | Platform |
| `idempotency_keys` | Prevents duplicate bookings on retry |
| `feature_flags`, `feature_flag_overrides` | Safe rollout during migration |
| `search_impressions_suppressed` | Paused-mentor demand signal |
| `account_deletion_requests` | User-chosen deletion |
| `legal_documents`, `user_legal_consents` | Terms versioning |

## Deferred — documented, not created

| Table | Trigger to build |
|---|---|
| `tags`, `user_tags` | AI matching work begins |
| `mentor_embeddings` | ~200+ mentors |
| `match_impressions`, `match_outcomes` | Build with the first ranked search |
| Payments tables | Separate discussion |
