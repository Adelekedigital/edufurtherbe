-- =============================================================================
-- 09_REPORTING
-- Curated views for internal team analytics.
--
-- WHY A SEPARATE SCHEMA RATHER THAN POINTING METABASE AT RAW TABLES:
-- the team queries stable, well-named views instead of coupling dashboards to
-- table internals. You can refactor `sessions` without breaking nine saved
-- questions. This matters more than the BI tool choice, and it's tool-agnostic —
-- switching off Metabase later costs nothing.
--
-- It's also what makes Metabase's natural-language querying actually work:
-- it reads your metadata and definitions, so well-described views produce far
-- better results than raw tables called credit_lots.
--
-- OPERATIONAL RULE: NEVER point a BI tool at the production primary. One
-- unindexed dashboard query scanning sessions will degrade live bookings. Use a
-- read replica (a config toggle on managed Postgres).
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS reporting;

-- -----------------------------------------------------------------------------
-- Mentor performance. Replaces the derived counters that used to be stored on
-- the `Mentor (front search)` table and drifted.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_mentor_performance AS
SELECT
  mp.user_id                                              AS mentor_id,
  u.first_name || ' ' || u.last_name                      AS mentor_name,
  mp.approval_status,
  mp.listing_status,
  mp.primary_study_country_code,
  count(s.id) FILTER (WHERE s.status = 'completed')       AS sessions_completed,
  count(s.id) FILTER (WHERE s.status = 'cancelled')       AS sessions_cancelled,
  count(s.id) FILTER (WHERE s.status = 'no_show')         AS sessions_no_show,
  count(s.id)                                             AS sessions_total,
  round(
    100.0 * count(s.id) FILTER (WHERE s.status = 'completed')
    / nullif(count(s.id), 0), 1)                          AS completion_rate_pct,
  count(DISTINCT r.id)                                    AS reviews_received,
  round(avg(r.nps_recommend_score), 2)                    AS avg_nps,
  round(avg((r.communication_rating + r.knowledge_rating
           + r.practicality_rating + r.support_rating) / 4.0), 2) AS avg_rating
FROM mentor_profiles mp
JOIN users u   ON u.id = mp.user_id
LEFT JOIN sessions s ON s.mentor_id = mp.user_id
LEFT JOIN reviews  r ON r.reviewed_for = mp.user_id AND r.deleted_at IS NULL
WHERE mp.deleted_at IS NULL
GROUP BY mp.user_id, u.first_name, u.last_name, mp.approval_status,
         mp.listing_status, mp.primary_study_country_code;

COMMENT ON VIEW reporting.v_mentor_performance IS
  'One row per mentor: session counts by outcome, completion rate, review stats.';


-- -----------------------------------------------------------------------------
-- Session funnel by month.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_session_funnel AS
SELECT
  date_trunc('month', s.created_at)                       AS month,
  count(*)                                                AS bookings_created,
  count(*) FILTER (WHERE s.status = 'confirmed')          AS confirmed,
  count(*) FILTER (WHERE s.status = 'completed')          AS completed,
  count(*) FILTER (WHERE s.status = 'cancelled')          AS cancelled,
  count(*) FILTER (WHERE s.status = 'declined')           AS declined,
  count(*) FILTER (WHERE s.status = 'expired')            AS expired,
  count(*) FILTER (WHERE s.status = 'no_show')            AS no_show,
  round(100.0 * count(*) FILTER (WHERE s.status = 'completed')
        / nullif(count(*), 0), 1)                         AS completion_rate_pct
FROM sessions s
GROUP BY 1 ORDER BY 1 DESC;


-- -----------------------------------------------------------------------------
-- Cancellation reasons — this is what reason_code was FOR.
-- "What % of mentor-side cancellations are scheduling conflicts" decides whether
-- you build reschedule flows. Impossible to answer from free text.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_cancellation_reasons AS
SELECT
  date_trunc('month', se.created_at)                      AS month,
  se.reason_code,
  CASE WHEN se.actor_id = s.mentor_id THEN 'mentor'
       WHEN se.actor_id = s.mentee_id THEN 'mentee'
       WHEN se.actor_id IS NULL       THEN 'system'
       ELSE 'admin' END                                   AS cancelled_by,
  count(*)                                                AS cancellations,
  round(avg(EXTRACT(EPOCH FROM (s.starts_at - se.created_at)) / 3600.0), 1)
                                                          AS avg_hours_notice
FROM session_events se
JOIN sessions s ON s.id = se.session_id
WHERE se.to_status IN ('cancelled', 'declined')
GROUP BY 1, 2, 3 ORDER BY 1 DESC, 4 DESC;


-- -----------------------------------------------------------------------------
-- Credit consumption by source. Becomes financially important once credits are
-- purchasable.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_credit_consumption AS
SELECT
  date_trunc('month', ct.created_at)                      AS month,
  cl.source,
  count(DISTINCT ct.user_id)                              AS users_affected,
  sum(ct.delta) FILTER (WHERE ct.delta > 0)               AS credits_granted,
  abs(sum(ct.delta) FILTER (WHERE ct.delta < 0))          AS credits_consumed,
  sum(cl.unit_cost_cents * abs(ct.delta)) FILTER (WHERE ct.delta < 0) / 100.0
                                                          AS revenue_recognised
FROM credit_transactions ct
JOIN credit_lots cl ON cl.id = ct.credit_lot_id
GROUP BY 1, 2 ORDER BY 1 DESC;


-- -----------------------------------------------------------------------------
-- Referral conversion — measures the activation funnel the credit gate creates:
-- signup -> baseline credit -> first session -> invite -> unlock.
-- If people burn the baseline credit and never invite, the prompt is mistimed.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_referral_conversion AS
SELECT
  date_trunc('month', r.invited_at)                       AS month,
  count(*)                                                AS invites_sent,
  count(*) FILTER (WHERE r.signed_up_at IS NOT NULL)      AS signed_up,
  count(*) FILTER (WHERE r.qualified_at IS NOT NULL)      AS qualified,
  round(100.0 * count(*) FILTER (WHERE r.signed_up_at IS NOT NULL)
        / nullif(count(*), 0), 1)                         AS signup_rate_pct,
  round(100.0 * count(*) FILTER (WHERE r.qualified_at IS NOT NULL)
        / nullif(count(*) FILTER (WHERE r.signed_up_at IS NOT NULL), 0), 1)
                                                          AS qualification_rate_pct
FROM referrals r
GROUP BY 1 ORDER BY 1 DESC;


-- -----------------------------------------------------------------------------
-- Blocked bookings — is the concurrency limit protecting capacity or
-- strangling engagement?
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_blocked_bookings AS
SELECT
  date_trunc('week', ba.created_at)                       AS week,
  ba.outcome,
  count(*)                                                AS attempts,
  count(DISTINCT ba.mentee_id)                            AS distinct_mentees
FROM booking_attempts ba
WHERE ba.outcome <> 'created'
GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC;


-- -----------------------------------------------------------------------------
-- Unmet demand from paused mentors. Feeds both the admin dashboard and the
-- mentor's own "12 mentees searched while you were paused" stat.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_suppressed_demand AS
SELECT
  sis.mentor_user_id,
  u.first_name || ' ' || u.last_name                      AS mentor_name,
  sis.suppression_reason,
  date_trunc('week', sis.created_at)                      AS week,
  count(*)                                                AS suppressed_impressions,
  count(DISTINCT sis.searcher_id)                         AS distinct_searchers
FROM search_impressions_suppressed sis
JOIN users u ON u.id = sis.mentor_user_id
GROUP BY 1, 2, 3, 4 ORDER BY 4 DESC, 5 DESC;


-- -----------------------------------------------------------------------------
-- Review coverage. Baseline at migration: 53 reviews / 935 sessions = 5.7%.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_review_coverage AS
SELECT
  date_trunc('month', s.starts_at)                        AS month,
  count(*)                                                AS completed_sessions,
  count(r.id)                                             AS reviews_left,
  round(100.0 * count(r.id) / nullif(count(*), 0), 1)     AS review_rate_pct
FROM sessions s
LEFT JOIN reviews r ON r.session_id = s.id AND r.deleted_at IS NULL
WHERE s.status = 'completed'
GROUP BY 1 ORDER BY 1 DESC;

COMMIT;
