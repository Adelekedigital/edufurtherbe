-- =============================================================================
-- 07_COMMUNICATIONS
-- Notifications, multi-channel delivery, messaging.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- notifications — legacy Notifications (681 rows).
--
-- Legacy had FIVE columns doing one job: Receiver, Receiver(list of users),
-- Seen(list of users), Seen receiver, Seen sender.
--
-- THE TYPED-FK RULE (asked directly; this is the decision boundary):
--
--   Add a typed FK column ONLY when you need one of two things:
--     1. CASCADE/CLEANUP — when the entity is deleted or hidden, related
--        notifications must be found and handled
--     2. BULK JOIN FOR LIST RENDERING — the inbox query joins to it per row
--
--   EVERYTHING ELSE GOES IN `context` jsonb AND GETS NO COLUMN.
--
-- Three qualify: session_id, review_id, conversation_id. Credit grants, referral
-- qualifications, mentor approvals, penalty warnings, milestone completions —
-- none get columns. They're terminal; nothing needs to find them later.
--
-- PLATFORM-WIDE ANNOUNCEMENTS: all three FKs null, type='platform_announcement',
-- content in `context`. Normal case, no special handling.
--
-- Adding a fourth typed FK later is a CHEAP migration (nullable column, no
-- backfill). Not forbidden — just rare and justified by rule 1 or 2.
--
-- DISCIPLINE THAT KEEPS THIS HONEST: `context` is read by TEMPLATES, never by
-- business logic. The moment a rule branches on a context value, that field has
-- earned a real column.
-- -----------------------------------------------------------------------------
CREATE TABLE notifications (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  type             text NOT NULL,               -- 'session_booked', 'credit_granted', ...
  title            text NOT NULL,
  body             text,

  session_id       uuid REFERENCES sessions(id) ON DELETE CASCADE,
  review_id        uuid REFERENCES reviews(id) ON DELETE CASCADE,
  conversation_id  uuid,                        -- FK added after conversations below

  context          jsonb NOT NULL DEFAULT '{}',

  created_by       uuid REFERENCES users(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  legacy_bubble_id text UNIQUE
);

CREATE INDEX idx_notifications_type ON notifications (type, created_at DESC);
CREATE INDEX idx_notifications_context ON notifications USING gin (context jsonb_path_ops);


-- -----------------------------------------------------------------------------
-- notification_recipients — per-user delivery and read state.
-- -----------------------------------------------------------------------------
CREATE TABLE notification_recipients (
  notification_id uuid NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  read_at         timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (notification_id, user_id)
);

CREATE INDEX idx_notification_recipients_unread ON notification_recipients (user_id, created_at DESC)
  WHERE read_at IS NULL;


-- -----------------------------------------------------------------------------
-- notification_outbox — DISPATCH QUEUE.
--
-- OWN THE RECORD, RENT THE DELIVERY. Business logic writes to notifications +
-- outbox in ONE transaction; a dispatcher fans out to channels. Business logic
-- never calls a vendor SDK, so swapping providers touches one module.
--
-- NOT USING NOVU THIS PHASE: 681 notifications doesn't justify it. Two tables +
-- a dispatcher + web-push + the existing email sender. Add Novu when digesting
-- and preference management are actually wanted.
-- -----------------------------------------------------------------------------
CREATE TABLE notification_outbox (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  notification_id  uuid NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  channel          delivery_channel NOT NULL,

  status           text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','sent','delivered','failed','skipped')),
  attempts         int NOT NULL DEFAULT 0,
  scheduled_for    timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz,
  delivered_at     timestamptz,
  error_detail     text,                        -- bounce reason, WA error, push 410

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_outbox_pending ON notification_outbox (scheduled_for)
  WHERE status = 'pending';
CREATE INDEX idx_outbox_notification ON notification_outbox (notification_id);


-- -----------------------------------------------------------------------------
-- notification_channel_preferences
-- Sensible defaults: reminders -> whatsapp + push; confirmations -> email +
-- in_app; marketing -> email only, explicit opt-in for whatsapp.
-- -----------------------------------------------------------------------------
CREATE TABLE notification_channel_preferences (
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  notification_type text NOT NULL,
  in_app            boolean NOT NULL DEFAULT true,
  email             boolean NOT NULL DEFAULT true,
  push              boolean NOT NULL DEFAULT false,
  whatsapp          boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, notification_type)
);


-- -----------------------------------------------------------------------------
-- push_subscriptions — Web Push (VAPID).
--
-- iOS PWA REALITY CHECK (verified):
--   WORKS: Badging API on iOS 16.4+ for home-screen web apps. navigator
--     .setAppBadge() is exposed in Worker contexts, so the service worker's push
--     handler can update the badge. Permission to display the badge follows the
--     notification permission.
--   REQUIRED: home-screen install. Push does NOT work in a Safari tab, and iOS
--     gives no beforeinstallprompt event — you must teach Share > Add to Home
--     Screen manually. Expect drop-off. Permission request must come from a
--     user gesture.
--   NOT AVAILABLE: delivery receipts (a 201 from Apple's gateway means accepted,
--     not displayed), silent push.
--   KNOWN ISSUE: subscriptions go inactive after prolonged inactivity. Handle
--     410 Gone by marking dead, and RE-SUBSCRIBE ON EVERY APP OPEN, not just at
--     first permission grant.
--
-- CONSEQUENCE: email/WhatsApp must be a REAL channel, not a fallback afterthought.
-- For a booking confirmation, email is the reliable path; push is the enhancement.
-- -----------------------------------------------------------------------------
CREATE TABLE push_subscriptions (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint        text NOT NULL UNIQUE,
  p256dh_key      text NOT NULL,
  auth_key        text NOT NULL,
  user_agent      text,
  platform        text,
  last_success_at timestamptz,
  failure_count   int NOT NULL DEFAULT 0,
  revoked_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_push_subscriptions_user ON push_subscriptions (user_id)
  WHERE revoked_at IS NULL;


-- -----------------------------------------------------------------------------
-- whatsapp_templates
--
-- THE STRUCTURAL CONSTRAINT: business-initiated WhatsApp messages must use
-- PRE-APPROVED templates. You cannot send arbitrary text. Submit
-- "Your session with {{1}} starts in {{2}}" to Meta, wait for review, then fill
-- variables.
--
-- CATEGORY MATTERS FOR COST AND IS ENFORCED BY META, NOT YOU. Booking
-- confirmations and reminders are `utility` (cheap tier). ANY promotional
-- element reclassifies to `marketing` at roughly 10x — so keep referral prompts
-- OUT of utility templates. "Your session is confirmed — invite 3 friends for
-- free credits!" gets reclassified.
--
-- Provider: Meta Cloud API directly at this volume; BSP markup buys nothing yet.
-- Start Meta Business verification EARLY (days to weeks).
-- -----------------------------------------------------------------------------
CREATE TABLE whatsapp_templates (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  notification_type  text NOT NULL,
  meta_template_name text NOT NULL,
  language_code      text NOT NULL DEFAULT 'en',
  category           text NOT NULL CHECK (category IN ('utility','marketing','authentication')),
  status             text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','approved','rejected','paused')),
  variable_mapping   jsonb NOT NULL DEFAULT '{}',  -- context fields -> {{1}}, {{2}}
  approved_at        timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (meta_template_name, language_code)
);


-- =============================================================================
-- MESSAGING
--
-- BUILD, DON'T OUTSOURCE. At 13 conversations and 44 messages, Stream (~$400+/mo)
-- or Sendbird would be the most over-engineered decision in this migration. The
-- use case is low-volume transactional 1:1 messaging around a booking — closer
-- to email threading than community chat. Supabase Realtime gives live delivery
-- by subscribing to Postgres changes on `messages`: no extra service, no extra
-- cost, no data leaving the database. (LISTEN/NOTIFY + a WebSocket layer is the
-- equivalent if not on Supabase.)
--
-- NAMING: the legacy names were INVERTED relative to normal usage —
-- `messageThreads` held individual messages. Renamed to conversations/messages
-- so nobody misreads it.
--   messageStarters -> conversations
--   messageThreads  -> messages
-- =============================================================================

-- -----------------------------------------------------------------------------
-- conversations
-- messageRequestAccept -> status. The pending/accepted gate is worth keeping:
-- mentors shouldn't receive unsolicited DMs.
-- -----------------------------------------------------------------------------
CREATE TABLE conversations (
  id                   uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  status               conversation_status NOT NULL DEFAULT 'pending',
  last_message_at      timestamptz,
  last_message_preview text,                    -- maintain with a TRIGGER, not app code
  created_by           uuid REFERENCES users(id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  deleted_at           timestamptz,
  legacy_bubble_id     text UNIQUE
);

CREATE INDEX idx_conversations_recent ON conversations (last_message_at DESC)
  WHERE deleted_at IS NULL;

ALTER TABLE notifications
  ADD CONSTRAINT fk_notifications_conversation
  FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;


-- -----------------------------------------------------------------------------
-- conversation_participants
--
-- last_read_at on the PARTICIPANT beats a per-message read flag:
--   unread = COUNT(*) FROM messages WHERE sent_at > last_read_at
--
-- Legacy `receivedBy` on each message DISAPPEARS — recipients are everyone in
-- the conversation who isn't the sender. Storing it per message is redundant and
-- breaks the moment a conversation has three people.
-- -----------------------------------------------------------------------------
CREATE TABLE conversation_participants (
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  joined_at       timestamptz NOT NULL DEFAULT now(),
  last_read_at    timestamptz,
  muted_at        timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, user_id)
);

CREATE INDEX idx_conversation_participants_user ON conversation_participants (user_id);


-- -----------------------------------------------------------------------------
-- messages
--
-- message_type + payload ARE ADDED NOW EVEN THOUGH ONLY 'text' IS USED.
-- Two columns, zero cost, and they make slash commands (/booking -> a tappable
-- booking card) a FRONTEND project later rather than a data migration.
--
-- IF/WHEN ACTION CARDS ARE BUILT, three rules keep it sane:
--   1. Card state is DERIVED from the referenced session_id, never stored in
--      payload. No dual writes, no drift.
--   2. Cards EXPIRE. A booking offer from three weeks ago shouldn't be tappable.
--   3. Every card action goes through the SAME ENDPOINT as the normal UI flow.
--      The moment there are two ways to create a booking, they diverge.
--
-- This is also why building messaging in-house is right: Stream and Sendbird
-- support custom message types, but you'd be round-tripping your own booking
-- state through their infrastructure and reconciling it. Here, an action card is
-- a foreign key.
-- -----------------------------------------------------------------------------
CREATE TABLE messages (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_id        uuid NOT NULL REFERENCES users(id),
  message_type     message_type NOT NULL DEFAULT 'text',
  body             text,
  payload          jsonb,
  sent_at          timestamptz NOT NULL DEFAULT now(),
  edited_at        timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  legacy_bubble_id text UNIQUE
);

CREATE INDEX idx_messages_conversation ON messages (conversation_id, sent_at DESC)
  WHERE deleted_at IS NULL;


-- -----------------------------------------------------------------------------
-- message_attachments
--
-- Reuses the upload pipeline needed for intake form file responses anyway.
--
-- REQUIRED REGARDLESS OF SCANNER: direct-to-storage presigned uploads (client ->
-- storage, never through the API), 10MB cap, MIME allowlist, RANDOMISED storage
-- keys (not user-supplied filenames), and serve from a SEPARATE DOMAIN so a
-- malicious HTML upload can't run in your session context.
--
-- SCANNING: ClamAV is GPL-2.0, free at any scale, Cisco/Talos-maintained,
-- signature updates free via freshclam. Budget ~1-1.5GB RAM for resident clamd
-- (run it on the worker box, not the API box). The realistic threat isn't
-- sophisticated malware — it's a mentor's antivirus flagging your platform as
-- the source of something dumb. Mentees will send CVs and personal statements
-- to strangers.
-- -----------------------------------------------------------------------------
CREATE TABLE message_attachments (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  message_id        uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  storage_key       text NOT NULL,
  file_name         text NOT NULL,
  mime_type         text NOT NULL,
  size_bytes        bigint NOT NULL,
  width             int,
  height            int,
  duration_seconds  int,
  thumbnail_key     text,
  virus_scan_status scan_status NOT NULL DEFAULT 'pending',
  uploaded_by       uuid NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_message_attachments_message ON message_attachments (message_id);


-- -----------------------------------------------------------------------------
-- last_message_preview maintained by TRIGGER, not application code.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_conversation_last_message()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE conversations
  SET last_message_at = NEW.sent_at,
      last_message_preview = left(coalesce(NEW.body, '[attachment]'), 140),
      updated_at = now()
  WHERE id = NEW.conversation_id;
  RETURN NEW;
END
$$;

CREATE TRIGGER trg_conversation_last_message
  AFTER INSERT ON messages
  FOR EACH ROW EXECUTE FUNCTION update_conversation_last_message();

SELECT attach_updated_at_triggers();

COMMIT;
