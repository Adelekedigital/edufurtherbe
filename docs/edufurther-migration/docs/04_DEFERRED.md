# Deferred Work

Things deliberately not built, why, and what should trigger building them.

The point of this document: **every item here has a defined escape hatch.** None
of them require a redesign later — that's what makes deferring them safe.

---

## 1. Matching taxonomy (`tags` / `user_tags`)

**Deferred.** `service_offerings` handles it for now.

### Why it's safe to defer

The structural fix is already in place: mentee needs and mentor offerings
reference the **same** `service_offerings` lookup. Basic matching works today as
a join:

```sql
SELECT m.mentor_user_id, COUNT(*) AS overlap
FROM mentee_goal_needs g
JOIN mentor_service_offerings m USING (service_offering_id)
WHERE g.user_id = :mentee
GROUP BY 1 ORDER BY overlap DESC;
```

The legacy problem wasn't the absence of a tags table — it was that Bubble had
**two separate option sets with no mapping between them**. That's fixed.

### The eventual shape

```sql
tags (
  id, type, slug, display_name, parent_tag_id, sort_order, is_active
)
-- type: field_of_study | degree_level | country | scholarship_type
--     | mentorship_need | industry | test_type

user_tags (
  user_id, tag_id,
  relation,   -- offers | seeks | has_experience
  weight,
  source      -- self_declared | derived | admin_assigned
)
```

`relation` is what makes one table serve both sides. `parent_tag_id` gives
hierarchy, so "Computer Science" matches a mentor tagged "Engineering" at a
discount. `source = 'derived'` lets you infer tags from behaviour later without
polluting what the user declared.

### Migration path

`service_offerings` → `tags WHERE type = 'mentorship_need'`.
`mentor_service_offerings` → `user_tags WHERE relation = 'offers'`.
`mentee_goal_needs` → `user_tags WHERE relation = 'seeks'`.
Same for `scholarship_programs`, `degree_levels`, `countries`.

One migration, no redesign.

**Trigger:** when AI matching work begins.

---

## 2. AI / vector matching

**Deferred.** The `vector` extension line is commented in `00_foundation.sql`.

### Why

At 44 mentors the corpus is too small for embeddings to beat good filters plus
sorting by rating. Semantic search adds value around a few hundred mentors.

### The architectural constraint that won't change

**Embeddings alone will not work.** Semantic similarity cannot enforce hard
constraints. "Mentor must have studied in Canada, speak French, and be accepting
bookings" is a filter, not a similarity score — an embedding model will happily
return a highly-similar mentor in Australia who's unavailable.

The pattern that works:

```
1. SQL WHERE  → hard constraints (listed, approved, country, language)
2. Vector     → rank the survivors by semantic fit
3. Business   → boost by rating, completion rate, recency
```

Which means **you need structured fields regardless of how good the model gets.**
That's why the schema invests in `service_offerings`, `user_languages`, and
`education_entries.country_code` rather than leaning on free text.

### The eventual shape

```sql
CREATE EXTENSION vector;

CREATE TABLE mentor_embeddings (
  mentor_user_id uuid PRIMARY KEY REFERENCES mentor_profiles(user_id),
  embedding      vector(1536),
  source_text    text,          -- exactly what was embedded
  model_version  text,          -- 'text-embedding-3-small'
  generated_at   timestamptz,
  created_at, updated_at
);
CREATE INDEX ON mentor_embeddings USING hnsw (embedding vector_cosine_ops);
```

`source_text` and `model_version` are the fields people omit and regret. When you
change models — and you will — you need to know which rows were embedded with
what, and re-embed selectively. Storing the source text means re-embedding
doesn't require reconstructing the input from six joined tables.

### Two tables worth building EARLIER than the rest

```sql
match_impressions (
  id, mentee_id, mentor_user_id, rank_position,
  query_context jsonb, algorithm_version, shown_at
)
match_outcomes (
  impression_id, action,   -- viewed | booked | dismissed
  acted_at
)
```

**This is training signal you cannot create retroactively.** Without it you'll
have no way to measure whether AI matching actually beats sorting by rating. Two
tables and an insert on search.

**Build these with the first ranked search**, not with the embeddings.

### Explainability is not optional

Mentees will ask why they were shown someone, and "the embedding was close" is
not an answer. Keep the structured signals available so you can render "matches
your target country and speaks Yoruba" alongside the ranking.

**Trigger:** ~200+ mentors, or when ranked search ships.

---

## 3. Payments

**Deferred to a separate discussion.**

### Why the credit model already accommodates it

`credit_lots` was designed for this. `unit_cost_cents` and `currency` sit unused
at zero until the day they aren't. The key property:

- **Free credits expire monthly** (`expires_at` set)
- **Paid credits never expire** (`expires_at` null, enforced by
  `CHECK (source <> 'purchase' OR expires_at IS NULL)`)

Expiring something a user bought is a chargeback and, in several jurisdictions,
unlawful. A single balance couldn't represent both lifecycles — lots can.

FIFO consumption already orders expiring-first, so free credits burn before paid.

### What payments will add

```
payment_methods    -- user_id, provider, external_id, last4, brand, is_default
transactions       -- user_id, amount_cents, currency, status,
                   -- provider_reference, credit_lot_id
payouts            -- mentor payouts, if mentors get paid
invoices           -- if B2B or institutional billing
refunds            -- linked to transactions and credit_transactions
```

### What to decide before building

- Do mentors get paid, or is this mentee-side only? (Changes everything.)
- Multi-currency, or USD only with local display?
- Nigeria-relevant rails — Paystack and Flutterwave have far better local
  coverage than Stripe.
- Tax and invoicing obligations by jurisdiction.

**Prerequisite already built:** `idempotency_keys`. Duplicate charge prevention
must exist before money moves.

**Trigger:** business decision.

---

## 4. Multi-tenancy

**Deferred, deliberately not pre-built.**

### Revised reasoning

Standard advice says add `organization_id` now because it's cheap. It isn't
free: it means every query, every index, and every RLS policy carries it —
an indefinite tax paid for a maybe.

### Retrofit path

```sql
CREATE TABLE organizations (...);
INSERT INTO organizations (name) VALUES ('EduFurther');  -- default
ALTER TABLE <each> ADD COLUMN organization_id uuid REFERENCES organizations(id);
UPDATE <each> SET organization_id = <default>;
ALTER TABLE <each> ALTER COLUMN organization_id SET NOT NULL;
```

A weekend on a schema this size.

### The one thing to avoid meanwhile

**Globally-unique human-readable keys that would need to become tenant-scoped.**
Practically: don't make referral codes, session-type slugs, or similar
identifiers globally unique in a way you'd have to break later. UUIDv7 PKs
already sidestep most of this.

**Trigger:** cohorts, university partnerships, or white-label deals.

---

## 5. Caching layer (Redis)

**Deferred.**

### Why

At 1,200 users every table fits in memory several times over. Indexes plus
TanStack Query cover it. Adding Redis now buys a cache-invalidation bug, not
speed.

### What would go in it, when the time comes

**Good candidates:** computed availability slots (expensive to generate, changes
rarely, 60s TTL), mentor search results (5min TTL), aggregate counts.

**Never cache:** credit balances, session status, unread counts. Anything where
staleness produces a *wrong action* — a double-booking, a phantom credit.

**Trigger:** p95 on a real endpoint crosses ~300ms under actual traffic, with
`pg_stat_statements` data confirming it's a query problem and not an N+1.

---

## 6. Dedicated search (Typesense / Meilisearch)

**Ruled out for this phase, explicitly.**

Postgres FTS + `pg_trgm` is enough until roughly 10,000 mentors. At 44 a 6-way
join is sub-millisecond.

**Escalation path, in order:**
1. Current: indexed joins (in the DDL)
2. Next: `MATERIALIZED VIEW` + `REFRESH CONCURRENTLY` — one-line change, no
   application rewrite
3. Only then: a dedicated search service

Each step is a strictly larger commitment. Don't skip to step 3.

**Trigger:** step 2 is insufficient and faceted search with typo tolerance is a
product requirement, not a nice-to-have.

---

## 7. Third-party notification platform (Novu)

**Deferred.**

Own the record (`notifications` + `notification_recipients`), rent the delivery
(`notification_outbox` → web-push + email + WhatsApp). At 681 notifications, Novu
adds an operational dependency for capabilities you aren't using.

**What Novu would add:** digesting/batching, a workflow editor, a prebuilt inbox
component, user-facing preference management.

**The outbox pattern means adopting it later touches one dispatcher module.**

**Trigger:** you want digesting ("3 new messages" instead of 3 notifications) or
user-facing preference management beyond `notification_channel_preferences`.

---

## 8. Native app (Capacitor)

**Deferred.**

### The known weakness of the PWA plan

iOS push requires manual Add-to-Home-Screen, with no `beforeinstallprompt` event.
Predictable drop-off, and you can't fix it from the web side.

### What Capacitor changes

Same React codebase in a native shell. Real APNs push, no install friction, app
store presence, delivery receipts. Costs $99/yr Apple + $25 Google, plus ongoing
app review.

### Keep the port clean

**Don't use Capacitor-specific storage APIs** (`@capacitor/preferences`) in web
code. Otherwise the port stops being a few weeks of work.

**Trigger:** measure iOS home-screen install rate and notification opt-in after
launch. If both are poor and notifications correlate with retention, this becomes
worth the weeks.

---

## 9. Award verification

**Deferred — option A (self-reported) chosen for this phase.**

Columns exist (`verification_status`, `evidence_url`, `verified_at`,
`verified_by`, `rejection_reason`) and default to `unverified`. Nothing renders a
checkmark.

**Why deferred:** every verified claim is manual admin work and the queue never
empties.

**Enabling later is a feature flag, not a migration.** The likely next step is
option B — verify on request, mentor uploads evidence, badge appears if approved.
Optional work that scales with demand rather than volume.

**Trigger:** awards demonstrably drive booking decisions, or a false-claim
incident.

---

## 10. Slash commands / action cards in messaging

**Deferred, but the schema is ready.**

`messages.message_type` and `messages.payload` exist now. Two columns, zero cost.

### What it would look like

`/booking` produces a message with `message_type = 'action_card'`:

```json
{
  "card": "booking_offer",
  "session_id": "...",
  "session_type_id": "...",
  "slots": ["2026-08-05T14:00:00Z", "2026-08-05T16:00:00Z"],
  "expires_at": "2026-08-03T00:00:00Z"
}
```

Frontend renders buttons instead of a text bubble.

### Three rules if it's built

1. **Card state is derived** from the referenced `session_id`, never stored in
   payload. The card holds the FK; render state by reading the session. No dual
   writes, no drift.
2. **Cards expire.** A booking offer from three weeks ago shouldn't be tappable.
3. **Every card action goes through the same endpoint as the normal UI flow.**
   The moment there are two ways to create a booking, they diverge.

**Feasibility:** parsing and rendering is a few days. The hard part is state, and
the three rules above are what keep it manageable.

**Trigger:** product decision.

---

## 11. Composio replacement

**Not deferred exactly — the exit path is documented.**

### The known constraint

On Composio's self-serve plans, credentials pass through Composio's cloud. Even
with your own OAuth app, their backend callback URL is what gets registered with
the provider. Self-hosting is Enterprise-only, through sales.

So users' calendar tokens live in a third party's infrastructure, and users see
Composio's domain during OAuth unless you're on a white-label plan.

### Alternatives, if that becomes unacceptable

| Option | Fit |
|---|---|
| **Nango** | Open source, self-hostable, ~900 APIs, code-first, white-label auth. Strongest fit if you want to own the tokens. |
| **Google Calendar API direct** | No vendor, no per-call cost, full control. More code, and you own token refresh — but not much code for one provider. |
| **Paragon** | Self-hostable runtime, but Enterprise-only with license checks phoning home. |

### Why the switch is cheap

`calendar_connections` stores a reference, not tokens. Switching touches one
table and one service module.

**Note:** no platform bypasses Google verification if you want your own brand on
the consent screen. Calendar scopes are *sensitive* tier — verification without
the CASA assessment. Start it early regardless of provider.

**Trigger:** token custody becomes a compliance requirement, or Composio pricing
changes unfavourably.

---

## 12. Retention and archival jobs

**Schema supports it; jobs not written.**

Decide the numbers now — far easier than at 50M rows.

| Table | Suggested retention | Why |
|---|---|---|
| `auth_codes` | 90 days | Contains IPs (GDPR personal data) |
| `idempotency_keys` | 24 hours | Already has `expires_at` |
| `booking_attempts` | 1 year | Analytics value decays |
| `search_impressions_suppressed` | 6 months | Rolled up weekly anyway |
| `audit_log` | 24 months, then cold storage | Compliance; consider monthly partitioning |
| `notification_outbox` | 90 days after delivery | `notifications` keeps the record |
| `outbox_events` | 30 days after send | Pure dispatch bookkeeping |

**`audit_log` grows unbounded.** Not a problem at this scale, but plan monthly
partitioning before it becomes one.

**Trigger:** write these before launch. They're cron jobs, not a project.
