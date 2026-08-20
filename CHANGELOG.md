# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`.github/workflows/release.yml` parses the section matching the tag being
released. A tag with no matching section here fails the release job.

## [Unreleased]

### Fixed

- **A cancelled session no longer holds the mentor's hour forever.** It was
  hidden from the grid while `sessions_no_mentor_double_booking` — built over
  `LIVE_STATUSES`, which excludes `cancelled` — would have accepted a booking
  for it anyway, and nothing anywhere could release it. A mentee could book and
  cancel repeatedly and empty a mentor's calendar an hour at a time: the same
  shape `withdrawn` once had.

  The hour now returns by default. **A mentor who is not free says so** —
  `release_slot: false` on cancel writes an ordinary **availability exception**
  they can see beside their own blocks and remove whenever they like. It is
  mentor-only: a mentee's cancellation says nothing about the mentor's calendar,
  and their answer is ignored.

  It records *unavailability*, not a hold. Reserving time for a particular
  person is a different feature and this is not it.

  Nothing breaks: `release_slot` is optional, only `cancel` carries it, and
  `reason_code` stays optional on all four transitions.


### Added

- **A mentor's own calendar is subtracted from what a mentee may book.** If they
  have connected one, their Google busy periods are removed from `/slots`
  alongside the sessions this platform booked — the two are indistinguishable by
  the time they are subtracted, which is the point.

  **The check runs again inside the booking transaction.** Free/busy is
  eventually consistent, so a slot grid built seconds ago can miss a conflict
  written since; booking already asks `list_slots` for legality rather than
  re-deriving it, so the last look before the write came free.

  **It fails open, and ADR 0004's own words decide that** — free/busy is
  advisory and "must never be treated as the mechanism that prevents double
  booking". Google unreachable means slots are unchanged and the booking
  proceeds on declared availability alone. The exclusion constraint remains the
  thing that actually prevents a double booking. That record's open question is
  resolved and amended in place.

  **Unconnected mentors cost nothing** — no grant, no call — which is most of
  them. One request covers the whole span rather than one per day, so a 56-day
  grid is a single round trip.

  A revoked grant is recorded rather than retried forever: `invalid_grant`
  marks the connection `error` and clears the token, so the calls stop. A
  timeout or a rate limit does none of that.

- **A mentor can connect the calendar the platform reads their busy hours
  from.** `GET /api/v1/me/calendar` says whether one is connected,
  `GET /api/v1/me/calendar/connect` hands back the Google consent URL, and
  `DELETE /api/v1/me/calendar` disconnects.

  **Not the calendar the platform writes to.** ADR 0012 splits the two grants:
  `calendar.app.created` is given once by EduFurther's own account and creates
  every session's event — configuration, needing no table and no consent from
  anybody. `calendar.freebusy` is given by each mentor, reads only *when* they
  are busy, and is what `calendar_connections` holds.

  **The consent asks for one thing.** `calendar.freebusy` alone, so the screen
  a mentor sees says *"View your availability in your calendars."*
  `include_granted_scopes` is deliberately absent — it would let an earlier
  grant widen this one.

  **The refresh token is encrypted at rest** with Fernet, and the column is
  named `refresh_token_encrypted` so a reader who writes a plaintext one has
  made a mistake the name objects to. Disconnecting **destroys** the
  credential rather than only flipping a status; the row survives, marked
  revoked, so *that they once connected* stays answerable.

  **The OAuth `state` is sealed and expires in ten minutes**, which is the CSRF
  control: Google redirects a browser to the callback, so there is no bearer
  token on it and the mentor's identity has to travel in the `state` we issued.
  Without the seal an attacker could complete their own consent against a
  victim's session and attach **their** calendar to somebody else's account.

  **Reading a mentor's busy hours is not built yet** — that is the free/busy
  subtraction in `slot_store`, and it lands next against these rows. This is
  the grant, deliberately shipped on its own: reviewing a consent flow
  alongside a change to the most-tested query in the codebase means reviewing
  neither carefully.

  Set `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`,
  `CALENDAR_TOKEN_KEY` and `PUBLIC_BASE_URL`. All four or none — a partial
  configuration refuses with a `500` rather than failing further downstream
  with a message about whichever piece it reached first.

### Changed

- **A third party failing is now a `502` rather than a `500`.**
  `VenueUnavailableError` was deliberately left unmapped, with its docstring
  reserving the decision for "the release that wires this" — the consent
  callback is the first path that lets one escape to a client, so this is that
  release.

  It moved to `core.UpstreamError` to be mappable at all: `api` may not import
  `infra`, so while the class lived beside the adapters the transport layer
  could not name it and every escape fell through to the unmapped-error `500`.
  `AuthenticationError` moved for the identical reason. The old name is kept as
  an alias, because "the venue is unavailable" is what a room provider failing
  actually means at the call sites.

  **502, not 503**: the fault is always upstream. `503` would claim *this*
  service is unavailable and invite a retry against a request that fails the
  same way. Booking is unaffected — it still catches the error and continues
  without a link.

- **A confirmed session gets a calendar event, and a Meet link when that is its
  venue.** `GoogleCalendar` creates the event on the platform's **own** Google
  account and invites both parties as guests — neither completes an OAuth flow.

  `conferenceDataVersion=1` only when the venue is Meet: requesting a conference
  for a Daily session would put **two links** on the event, and the invitee
  clicks whichever renders first. A 200 with no link is treated as a failure,
  because that is exactly how the parameter being dropped looks.

  Plain REST through `httpx` rather than the Google SDK — two POSTs against a
  discovery mechanism and a large dependency tree.

  Set the three Google values to enable it; unset, sessions book exactly as
  before.

### Fixed

- **A cancelled session no longer leaves a live meeting in both calendars.**
  `external_calendar_event_id` was written by provisioning and read by nobody —
  harmless while no event was ever created, and a defect the moment one is.
  Declined, withdrawn, cancelled and expired all release it now; `completed` and
  `no_show` deliberately do not.

  The id is cleared **only on a successful removal**, so a failure keeps the
  handle for a later run — and the transition still succeeds, because a session
  cancelled in Google and confirmed here is worse than a stale event.

### Added

- **A `daily` session now has a room, and `/join` hands back the door.**
  `DailyRooms` creates a private room gated by `nbf`/`exp`, and
  `POST /sessions/{id}/join` mints a per-participant token and returns
  `<room url>?t=<token>`.

  **The token is returned, never stored.** A private room refuses anybody
  without one, so the stored address opens nothing — and keeping a token on the
  row would put two live bearer credentials per session into the database and
  every backup, outliving the session they open.

  The mentor's token is the owner's: on Daily that is who may admit, mute and
  end. Every other venue returns its stored URL unchanged.

  **`meeting_url` can be `null` on a success** — the arrival is recorded either
  way, and a venue that could not be reached is a different thing from being
  refused entry.

  Set `DAILY_API_KEY` to enable it; unset, sessions book exactly as before.
- **Response reminders fire, and a signed callback is what fires them.**
  `POST /api/v1/callbacks/reminders` — QStash publishes at booking, calls back
  when a reminder is due, and the callback **re-reads the request** before
  queueing anything.

  **Nothing is ever cancelled.** A reminder for a request already accepted,
  declined, withdrawn or expired does nothing and says so. The alternative makes
  four transitions responsible for unscheduling, and the bug is the one somebody
  forgets.

  **Safe to call twice**, which QStash does on retry: a partial unique index
  makes the second enqueue a no-op rather than a second identical email.

  Verification checks the issuer, the destination, **and a hash of the exact
  body** — without the last, anybody who observed one callback could replay its
  signature against a body of their choosing.

  Set `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY` and `PUBLIC_BASE_URL`. Unset,
  reminders never fire and nothing else changes.

### Fixed

- **An app now carries its own settings.** `create_app(settings)` stored them
  nowhere, so every dependency reading configuration called the process-wide
  `get_settings()` cache and ignored what the app was handed — which is how
  every test builds one.

- **`.env.example` documents `LOOPS_API_KEY` and the template maps**, which the
  outbox change should have added and did not: its edit was anchored on a line
  that only exists on another branch, and a `.replace` with no assert did
  nothing quietly.

### Added

- **People are told things.** `outbox_events` queues a message inside the
  transaction that caused it; the drain in `settle_sessions` sends it through
  Loops (ADR 0025).

  **One rule decides who hears it:** a message caused by somebody goes to the
  party who did *not* cause it, and a message caused by time going past goes to
  both. So an auto-confirming booking tells the **mentor** and an acceptance
  tells the **mentee** — the same rule twice, not two rules — and an expiry
  tells both, because nobody acted.

  **Queued, not sent, inside the request.** Sending inline would block a booking
  on a third party and lose the message on a crash. The outbox makes *this
  happened* and *this person was told* one transaction and one retryable unit.

  Retries are bounded at 5; a recipient with no address is **skipped** rather
  than failed; and the row's own id is the provider idempotency key, so a retry
  after a timeout replays rather than sends twice.

  Set `LOOPS_API_KEY` to enable delivery. Unset, the outbox still fills and
  drains — a missing key is visible in a table rather than in nobody's inbox.

  **Reminders are not in this**: they need QStash and a signed callback
  endpoint, which is the same machinery the Daily attendance webhook needs.

### Added

- **ADR 0025 — the notification architecture.** Two email senders split by use
  case (Emailit for auth, marketing and subscription; Loops for transactional),
  each on its own sending subdomain, with provider-prefixed template ids so a
  message can move between them by configuration alone.

  **One rule decides who is told:** a message caused by somebody goes to the
  party who did not cause it; a message caused by time going past goes to both.
  `expired` is the single exception, because no person produced it.

  **Reminders are QStash callbacks that re-read the session before sending**, so
  nothing is ever cancelled — a callback for a session called off simply does
  nothing. The hourly sweep cannot deliver a 30-minute reminder, and a tighter
  GitHub Actions cron is unreliable however short the interval.

  Nothing is built. This is the shape, and the record says what the build has to
  demonstrate.

### Fixed

- **Three OpenAPI tags had no description** — `public`, `availability` and
  `sessions`. Nothing compared the described list against the used one, so the
  spec rendered with empty headings. Both directions are now asserted.

- **A mentor's own offerings moved from the `users` tag to `session-types`.**
  Settled decision #64 gives everything outside the catalogue its domain name,
  and `me_session_types.py` said at the time that a tag *"may still earn its
  place once `DELETE` joins them"*. It has — eight endpoints now.

### Added

- **A mentor's status history can be filtered.**
  `GET /api/v1/admin/mentors/{id}/history` takes `status` (repeat it to widen)
  and a half-open `[since, until)` window of UTC instants.

  Half-open so two adjacent ranges partition the log — a closed upper bound
  returns a boundary event in both, and a reviewer paging month by month counts
  it twice. Instants rather than dates: this log has no owning mentor whose day
  it could mean, so a timezone-less value is refused rather than guessed.

- **A confirmed session is given somewhere to meet.** `MeetingRoom` and
  `Calendar` ports with null adapters, `DailyRooms` and `GoogleCalendar`
  skeletons, and the orchestration that decides which to call.

  **The provider decides two independent things.** `google_meet` asks the
  calendar for a conference and creates no room; `daily` creates a room and must
  **not** ask — asking would put two links on the event, with the invitee
  clicking whichever the client renders first; `custom` creates nothing and
  reuses the mentor's URL.

  **Provisioned at confirmation, which is two places** — booking for an
  auto-confirming offering, `/accept` for one that waits. A request that may be
  declined mints nothing.

  The venue is resolved through the same `COALESCE` the read models use, so what
  is provisioned agrees with what the mentee was shown.

  **Neither real adapter is built.** Google is blocked on `calendar_connections`,
  which does not exist; Daily is blocked on the spike. A failure to provision
  does not fail the booking — the session exists and holds its slot.

- **The notification shape, ahead of the providers.** A `Notification`
  vocabulary, `EMAIL_TEMPLATES` and `WHATSAPP_TEMPLATES` settings, and three
  adapters — `NullNotifier` (the default), `EmailitNotifier` and
  `ZernioNotifier`.

  **Neither real adapter is built, and both refuse loudly.** A stub that
  reported success would make the first real send the first time anybody
  discovered the shape was wrong, and the producer above it would look correct
  while delivering nothing.

  **The default delivers nothing and logs that it did.** That is the current
  state of the world: no email provider is configured, and WhatsApp can reach
  nobody until the deferred `phone_*` columns exist.

  Template ids are configured, not coded — every WhatsApp message this platform
  sends is business-initiated and so needs a Meta-approved template, and that
  approval has a lead time nobody here controls.

- **A session says when its join window opens and shuts, and whether each party
  has arrived.** `join_opens_at`, `join_closes_at`, and `joined_at` +
  `attendance_status` on both parties of `SessionRead`.

  **The window is sent rather than computed client-side.** The offsets are a
  product rule that will become a mentor preference, and a client hardcoding
  five and fifteen would drift the day that lands — silently.

  It is also what makes a waiting screen correct: *"your mentor can still join
  until 15:15"* is true, where *"wait up to fifteen minutes"* is wrong for
  somebody who arrived at 15:14.

  **"Your mentor has not joined yet" and "your mentor left" are different
  messages**, and the session's own status stays `confirmed` through both — so
  a client could not tell them apart without this.

  Correlated per side rather than joined, so a page of sessions stays one
  statement: a join to `session_participants` would multiply rows before the
  limit and lose them at the cursor boundary.

- **Every attendance outcome now records how it was reached.**
  `session_events.metadata` carries `{"evidence": "reported"}` — its first
  writer since the column shipped. When a provider starts reporting
  per-participant join and leave it carries `observed` instead.

  **Written before anything reads it, and that is the justification:** a session
  settled today cannot later be re-examined for whether anybody observed it. It
  is what will let a payout rule require observed attendance without a second
  status and without re-judging history.

- **A request nobody answers now dies, and gives the mentor's hour back.**
  `sessions.respond_by` plus an hourly sweep. `expired` gets its producer, and
  `SessionStatus.EXPIRED` stops being a value nothing could reach.

  **This closes a live defect**, not just a gap: the exclusion constraint covers
  `pending_mentor_approval`, so until now an abandoned request held a mentor's
  slot indefinitely — invisible on the grid and bookable by nobody.

  **`respond_by = starts_at - 6h`, on confirmation-required offerings only.**
  Null where nothing awaits an answer. Six rather than twenty-four because of
  the booking floor: the mentor's time to answer is
  `(starts_at - booked_at) - W`, so a 24-hour window against the 24-hour notice
  floor leaves **zero**. Six leaves eighteen.

  The deadline is returned on `SessionRead`, and stays on the row after the
  mentor answers as a record of how long they actually had.

  **Reminders are settled and unbuilt** — on booking, 24h before the deadline
  where the lead allows it, 12h before. They need a notification channel; the
  deadline ships without them because freeing the slot is the half that does not
  depend on telling anybody.

- **A mentee's attendance rate, on the row where a mentor decides.**
  `SessionRead.mentee_attendance_rate` — a whole-number percentage of the
  sessions they booked that have finished, correlated per row so a page of
  requests is still one statement.

  **`null` means no data and a client must render it as "New mentee", never
  `0%`.** Zero says *never shows up*; every mentee's first booking is null. The
  API does not send the words: substituting them would be a display decision
  made in the wrong layer, in one language, that no client could change.

  **The side is load-bearing.** `session_stats.attendance_rate` now takes the
  side as a parameter — one function, two populations. A person who hosts
  diligently and books unreliably has two records, and pooling them would let
  the first flatter the second on exactly the card where it matters. The
  mentor's rate is unchanged and stays on their public profile.

- **Join-window attendance, and the outcome it decides.**
  `POST /api/v1/sessions/{id}/join` marks **you** present, from five minutes
  before the start to fifteen after. `completed` and `no_show` finally have a
  producer: `scripts/settle_sessions.py`, on an hourly schedule.

  **You can only mark yourself.** Attendance drives both parties' reliability
  figures, so marking somebody present is editing their record. Joining twice is
  safe and keeps the *first* arrival.

  **The window is half-open**, so joining and settling never claim the same
  instant — a sweep on the boundary would brand somebody absent in the second
  they were still allowed to arrive, and nothing later could undo it.

  **A session happened only if everybody came.** One party alone in a room is
  `no_show` whichever party it was; *which* one stays on the participant rows,
  because a session-level status cannot say both.

  **Migrated sessions are never rewritten.** The sweep settles `confirmed` rows
  only. Three migrated sessions are `completed` with somebody absent — they
  record what the legacy app believed, and correcting them would destroy the
  evidence that the two figures disagree.

- **The four session transitions.** `POST /api/v1/sessions/{id}/accept`,
  `/decline`, `/withdraw` and `/cancel`. `withdrawn` finally has a producer, and
  so do `declined` and `confirmed`-after-approval.

  **Four names for one table.** Who may take each action, from which state, and
  which reason codes they may give live in `domain/sessions.py`, so *a mentee may
  never accept their own request* is enforced once rather than hoped for four
  times. The wrong party gets `404` — the action's URL does not exist for them,
  the same answer `require_admin` gives a non-admin — and the right party in the
  wrong state gets `409` naming the state.

  **No session may be cancelled within ten minutes of its start**, or after it.
  The rule is in `domain` rather than in a trigger: a `CHECK` cannot express it,
  and a trigger would make every change to the number a migration.

  **Reason codes are restricted per party.** They drive refund policy, so a
  mentee free to send `mentor_unavailable` could claim a refund by choosing a
  value. A code your side may not give is a `422` naming it.

- **A mentee can book a session.** `POST /api/v1/sessions` — the first write to
  `sessions`, and the first table that serves every feature and belongs to none,
  `idempotency_keys`. See ADR 0024.

  **`starts_at` must be an instant `/slots` currently offers, to the second.**
  The endpoint asks `list_slots` rather than reimplementing it, so the notice
  window, the mentor's hours or the offering's own scheduling windows, blocked
  dates and existing bookings all apply with no second set of rules to disagree
  with. Everything the grid does not offer is one `422` — the client's answer to
  every reason is the same, which is to re-read `/slots`.

  **The status is the mentor's setting, not the mentee's choice.** Resolved
  config first, then the mentor, which is the first thing to read
  `session_type_booking_configs.requires_booking_confirmation` since it shipped.

  **`Idempotency-Key` is required, and scoped to you.** A retry replays the
  original response — the same session, the same `201`, plus
  `Idempotent-Replayed: true` — rather than booking a second hour. Reusing a key
  with a different body is a `422`. Keys are replayable for 24 hours and expire
  by query rather than by a sweep, so the table self-heals.

  The unique index is `(user_id, key)` where the canonical package has `key`
  alone, because a key row holds a stored response body and the lookup must be
  scoped to the caller. ADR 0024 records why a global key space is then a defect
  rather than a stricter rule.

  Nothing accepts, declines or expires a booking yet, so one that lands
  `pending_mentor_approval` holds its slot until that ships.


- **Per-offering scheduling windows, which replace general availability.**
  `session_type_scheduling_windows` — an offering with windows is bookable in
  those and nowhere else; one with none uses `availability_rules`, unchanged.
  See ADR 0023.

  The screen's copy says the windows *restrict* availability, and its own example
  shows why intersecting is wrong: a deliberate evening window against normal
  working hours yields zero slots and an empty calendar with nothing to explain
  it.

  **`availability_exceptions` still subtract, always** — windows replace
  availability, not unavailability. **A mentor with windows and no general
  availability is bookable**, which is newly reachable and not a
  misconfiguration.

  No window-management surface yet, and the offering read model does not yet say
  which mode it is in. Both ship together; ADR 0023 records the second as an
  obligation rather than an omission.


- **A mentor can manage the intake form their offering asks.** Four endpoints on
  `/api/v1/me/session-types/{id}/questions` — list, add, change, remove. This
  gives `session_type_questions` its first reader.

  Nested under the offering because the offering is what carries ownership: both
  ids are in the `WHERE` on every write, so a question is reached through the row
  that says whose it is rather than looked up and checked afterwards.

  **At most five questions per offering.** A product rule with no column to hold
  it — "at most five rows in a group" is neither a `CHECK`, which sees one row,
  nor a unique index, which enforces distinctness rather than cardinality. It is
  counted in the store, which races; the loser leaves one extra question rather
  than corrupting anything. The count is of *live* questions, so deleting frees a
  slot.

  **`multi_choice` is not selectable.** The column accepts it and
  `session_type_question_options` exists for its choices, but nothing can create
  an option yet — so the question would be one no mentee could answer. The
  refusal lifts when option management arrives.

  Deletion is soft, using the `deleted_at` the intake stack gave the table for
  exactly this: `intake_answers.question_id` restricts, so a form that could
  never change once anybody filled it in would not be a form.


- **The intake stack — four tables, landing together.**
  `session_type_questions`, `session_type_question_options`,
  `intake_submissions` and `intake_answers`, the four deferred out of
  `04_sessions.sql` when M4 shipped. No endpoints yet; those follow.

  They land as a unit because half of them is worse than none: a question
  definition with nowhere to store an answer is a schema asserting a feature
  nobody can use.

  **`session_type_question_options` ships with nothing writing it**, and so does
  `multi_choice`. The UI's intake screens use two of the three question types,
  but the package is canonical (ADR 0007) so dropping a table is an undeclared
  divergence rather than a deferral — and #100's completion made being early
  cheap: the sub-rule against an unused vocabulary value existed because
  `ALTER TYPE ... ADD VALUE` is permanent, and that no longer applies.

  **An answer restricts its question, while a question cascades from its
  offering.** That asymmetry is ADR 0013 applied literally — a question is
  meaningless without the offering that asks it, an answer is evidence of what
  was asked — and it is why `session_type_questions` carries `deleted_at`:
  retiring a question is how a mentor edits their form without being refused by
  every answer ever given to it.

  `exactly_one_answer_form` sums rather than chains, because
  `a IS NULL OR b IS NULL OR c IS NULL` permits an answer carrying nothing at
  all.

  No ADR: this follows canonical. The five differences — `text` + `CHECK`
  vocabularies, `ix_` names, per-table `updated_at` triggers, explicit
  foreign-key actions, surrogate keys — are standing rules this schema already
  applies everywhere.

### Changed

- **`session_types.category` becomes `service_offering_id`, and
  `application_stage` becomes a closed set.** Both shipped as free `text` with no
  constraint and no value in any row, withheld from the public contract because
  publishing them would have committed it to a shape nobody had designed. The UI
  designed both, so that argument lapsed rather than being overruled. See ADR
  0022.

  **Breaking on `/me/session-types`:** `category` was a free-text string and is
  now a `service_offering` object with `code` and `display_name`. The name
  changed because `service_offerings` has its own `category` column — a display
  grouping — so a foreign key called `category` would point at a table with a
  different `category` one join away.

  **Additive on the public endpoint:** `GET /users/{id}/session-types` gains
  `service_offering`, `application_stage` and `custom_stage_label`.

  `application_stage` takes five values — `early_exploration`, `drafting_stage`,
  `post_submission`, `revisions`, `other` — as `text` + `CHECK` with a `StrEnum`
  at the boundary. `other` carries its label in a new `custom_stage_label`, tied
  by a **symmetric** constraint: the one-directional form is satisfied by a null
  label and never requires the payload it exists to require, which is the same
  hole that let a mentor sit on a `custom` venue with nowhere to meet.

  `sessions.topic` is deliberately not converted. The same decision names it as
  the same taxonomy from the mentee side, but the ETL writes it from legacy on
  every migrated session — a data migration over real rows rather than a schema
  change over an empty column.

### Fixed

- **A session nobody was recorded at is `no_show`, not `completed`.** The
  settlement asked *is anybody absent*, which is the same question in the
  ordinary case and the wrong one at the edges: a session with one participant
  row — or with none — contains nobody absent and was reported as delivered. It
  now requires **both named parties** recorded present, which `sessions` can
  state because it is 1:1 by design.

- **A missed session names the party who missed it.** Every absence was filed as
  `mentee_no_show`, a mentor's included. These codes are what refund policy runs
  on, so naming the wrong party is the wrong answer to the question that decides
  a refund. Both absent stays null — no code can say it, and the participant
  rows already do.

- **Recording an arrival no longer reports success when it wrote nothing.**
  `POST /sessions/{id}/join` returned `{"joined": true}` when the update matched
  zero rows, so a caller could be told their arrival was recorded, walk away,
  and be settled as absent with nothing to appeal against.

- **A declined, withdrawn or expired request no longer blocks the mentor's
  calendar.** `slot_store` subtracted no status at all, which was right while
  none of the three was reachable and became a mentor-facing denial of service
  the moment the transitions shipped: a mentee could empty a calendar
  permanently by requesting every hour and withdrawing. A **cancelled** session
  still holds its hour — it was agreed and then called off, and the mentor
  usually cancelled because they are busy.

- **Booking now writes both `session_participants` rows**, in the same
  transaction as the session, which is what the model's docstring has always
  said and nothing did. The join-window attendance sweep needs them to exist.


## [0.1.0] - 2026-08-01

### Added

- Project skeleton: `src/app/{api,domain,infra,core}` with the layer boundary
  enforced by `scripts/check_layers.py`.
- Configuration through `core/config.py` only, rejecting misspelled
  `EDUFURTHER_` environment variables at startup.
- `GET /health` liveness endpoint.
- Full local gate via `scripts/check.py`, wrapped by `make check`.
- CI, security, and release workflows; pre-commit hooks including secret
  scanning and Conventional Commits.


### Added

- **`DELETE /api/v1/me/session-types/{id}` — a mentor can remove an offering.**

  Soft, never hard: `sessions.session_type_id` is `RESTRICT`, so an offering
  that was ever booked could not be hard-deleted anyway, and the row is what a
  past session still points at — which is what keeps that mentee's history
  readable. The reads stop returning it; the row survives.

  **Refused with `409` while sessions are still booked**, meaning
  `pending_mentor_approval` or `confirmed`. A cancelled or completed session
  does not hold an offering open — a mentor whose offering ran for a year must
  not be told it is permanently undeletable because it was once used.

  **The `409` carries no machine-readable reason code, reversing a settled
  answer whose premise lapsed.** It was to carry one so a client could tell it
  from the primary-offering refusal; that refusal left with
  `primary_session_type_id`. One refusal has nothing to be distinguished from,
  and the error envelope has no reason-code mechanism, so adding one would
  change a shared contract to serve a single caller. The RFC 9457 `type` slot is
  where it goes when a second reason exists — additive, so nothing here blocks
  it.

  Deleting frees the name immediately, and a switched-off offering is still
  deletable — pausing before deleting is the obvious order to work in.

- **`POST` and `PATCH /api/v1/me/session-types` — a mentor can create and change
  their own offerings.** The write surface these tables have been waiting for:
  every screen had a `GET` behind it and no offering could be created at all.

  **The offering and its booking config are written in one transaction.** Both
  read paths and `/slots` inner-join the config, so an offering without one is
  invisible everywhere and unbookable — and nothing writes a config on its own,
  so no endpoint could repair it.

  `min_notice_minutes` accepts **1440 to 4320**: the 24-hour platform floor, no
  same-day booking, and the current 72-hour ceiling. The column keeps its
  sanity-only `CHECK BETWEEN 0 AND 43200` — a database refuses what is
  impossible, an application refuses what is disallowed, and moving the range to
  `booking_policies` should be a config change rather than a migration.

  `is_active` is a `PATCH` field with no cascade, and **nothing refuses it any
  more** — while `trg_refuse_retiring_a_primary_offering` existed this toggle
  fired it and needed a `409` mapping or returned a 500. A switched-off offering
  stays editable, which is what switching it back on requires.

  **`meeting_venue` is not writable yet.** An offering is held on one of the
  mentor's conferencing options and nothing lists or creates those, so a new
  offering leaves the reference null and resolves through the mentor's default.
  Per-offering venue arrives with the surface that manages options.

  A duplicate live name is `409`, translated off the existing partial unique
  index rather than re-implemented — checking first and inserting after is the
  race the index exists to win. A deleted offering does not reserve its name. A
  caller with no mentor profile gets `404`: the read endpoint's empty page is a
  read-side argument, and there is no true empty answer to a write.

- **`mentor_conferencing_options` — what a mentor can host on, as a table.**

  `session_type_booking_configs.meeting_venue` was a label, and half its
  vocabulary named capabilities the platform does not have: `zoom` has no
  integration and nothing can mint a link, `custom` needs a URL and
  `mentor_profiles.custom_meeting_url` was deleted. Two of four values could not
  produce a joinable session, and one column could not distinguish *which
  provider* from *whether this mentor can host on it*. See ADR 0021.

  An offering references one through a **composite** foreign key,
  `(mentor_user_id, conferencing_option_id) → (user_id, id)`, which makes
  pointing at another mentor's option unrepresentable rather than merely refused.
  Null means *use my default*, and resolution is three steps — the offering's own
  option, the mentor's default, then `google_meet` — so `meeting_venue` is never
  null and stays a required field.

  The `custom_url` check is **symmetric**, closing a gap the old one-directional
  constraint left open: `custom` with no URL was permitted, leaving a mentor
  bookable with nowhere to meet.

  **Migrated offerings on `custom` are quarantined.** No URL exists to carry, so
  those mentors load on `google_meet` — which keeps them bookable — and are named
  in the migration output and the ETL report for follow-up. Reported rather than
  guessed, on the `CalendarSettings` precedent.

  **`SessionTypeRead.meeting_venue` narrows to a new `ConferencingProvider`**,
  which omits `zoom`. It was advertised and never producible; the response enum
  now describes what the data can hold. `sessions.meeting_provider` keeps
  `MeetingProvider` — a session that happened on a venue keeps naming it.

### Removed

- **`mentor_profiles.primary_session_type_id` is dropped, with
  `trg_refuse_retiring_a_primary_offering`.**

  D88 gave the pointer two jobs. The fallback lost its last member when
  `requires_booking_confirmation` went back to the mentor; the other — *the
  offering a mentee lands on, which also drives display order* — was never real.
  Nothing lands a mentee on it and nothing orders by it: `_live_session_types`
  orders by `name`, which is unique per mentor among live rows and therefore a
  total order rather than a merely stable one. Measured before deciding: three
  consumers in production code, and after the confirmation move, zero.

  **Retiring an offering is now refused by nothing.** The guard fired on `UPDATE`
  as well as delete, so the sanctioned *release the pointer, then retire* two-step
  is gone and deactivating is a plain `is_active` toggle — a `409` the `PATCH` and
  `DELETE` endpoints no longer have to translate, and would have returned a 500
  without.

  The guard's five tests are deleted with a record of what would bring each back,
  in `tests/integration/test_offering_retirement.py`, which asserts the inverse.
  Its trigger-inventory test asks `pg_trigger` directly, because behavioural tests
  would also pass against a trigger whose predicate had quietly stopped matching.

- **`default_meeting_venue` is removed from `GET /users/{id}/mentor-profile`.**
  **Breaking** — a client gets a missing key, not a null. It was read through the
  dropped pointer and venue has no mentor-level home, so a permanent null would
  assert *this mentor has no venue*, a claim about the mentor, when venue is a
  property of an offering. The owner reads it per offering in
  `/me/session-types`; the public offering endpoint is unchanged.

### Changed

- **`requires_booking_confirmation` returns to `mentor_profiles`, and the
  per-offering column becomes a nullable override where null means inherit.**

  D88 moved the setting onto `session_type_booking_configs` as part of one
  fallback mechanism covering three fields. All three left that mechanism, each
  differently — `meeting_venue` became per-offering and `NOT NULL` because its
  cascade had a reachable, empty bottom; `custom_meeting_url` was deleted because
  nothing read it; and this one comes back to the mentor. The premise failed,
  not any individual placement, and that is what is recorded so the idea is not
  re-proposed.

  D88's argument against a nullable boolean does not apply to the new shape and
  that is the whole point. *A boolean has no room for a third state, so null
  would be indistinguishable from false* was right about inheriting from a
  **primary offering**, because a mentor may hold live offerings and no primary.
  A mentor row always exists, so the terminus cannot be missing.

  `GET /users/{id}/mentor-profile` therefore reports
  `requires_booking_confirmation` as a bool for every mentor, where it previously
  reported `null` for one with no primary offering. Narrowing rather than
  breaking: the field never stops being present and a client that handled the old
  `null` still validates a bool. `default_meeting_venue` is unchanged and still
  nullable.

- **The fan-out in `profile_writer` is deleted; the toggle writes one row.** It
  existed only because the column had moved while this endpoint was still the
  mentor's only control over it, so a toggle had to be copied onto every live
  offering or it would answer 200 and change nothing anybody read. Two writers
  for one column is the duplication non-negotiable #8 calls a defect.

  This fixes a case that was previously a silent no-op: **a mentor with no
  offerings can now store the setting.** Under the fan-out there was nowhere for
  it to land, which is the ordinary onboarding order rather than an edge case.

  The ETL follows: `mentor_profiles` is written by the profile load and every
  migrated offering is left inheriting. Writing the mentor's own value down onto
  the offerings would reconcile perfectly and be wrong — each would become a
  permanent override that merely happens to agree, and the mentor's toggle would
  stop affecting anything a reader resolves.

  The migration seeds the mentor column from `bool_or` over their non-deleted
  offerings **before** clearing them. Reversing those two statements loses every
  mentor's setting with the row count unchanged and every constraint satisfied,
  so a test writes an offering at `true` under the prior revision and upgrades
  over it. `test_booking_confirmation_defaults_to_false`, deleted by D88's
  contract step, is restored with the column.

### Fixed

- **Two settled decisions contradicted #100, and the education list's order was
  asserted nowhere.**

  #100 replaced native PostgreSQL enums with `text` + `CHECK`, but #31 still
  mandated the enums and #30 still called `pg_enum` *the only way* a model
  declares a vocabulary. A reader hitting either was told to do the thing #100
  exists to stop, at the one moment it costs most — adding a new vocabulary,
  when the label becomes permanent. #31 is struck and points at #100; #30 is
  narrowed rather than struck, because its `StrEnum`-in-`domain` half is
  unaffected and `pg_enum` is still how the 17 existing types are declared.
  #31's own `Reopen if` had named this exact trigger, and `LEFT_EARLY` fired it.

  `tests/unit/test_mentor_search_order.py` becomes `test_statement_ordering.py`
  and gains the education list. Dropping its `id` tiebreak, or sorting by
  `date_start` before `date_end`, previously left the whole suite green — the
  third ordering in this milestone introduced deliberately, described in prose,
  and pinned by nothing. Both now fail.

- **`PATCH /mentor-profile` with an explicit `"requires_booking_confirmation":
  null` was an authenticated 500.** The field was typed `bool | None`, which
  never made it optional — every route dumps with `exclude_unset=True`, so
  omitting it already worked — and only bought the right to send a `null` that
  was forwarded to a `NOT NULL` column. It is now `bool`, and a null is a 422.

  Pre-existing, and fixed here because it is the field this release moves and
  the new fan-out would otherwise have needed a dead `None` branch to guard a
  case the boundary should never have admitted.

- **A mentor's booking-confirmation toggle wrote where nothing was about to
  read.** `PATCH /mentor-profile` set `requires_booking_confirmation` on
  `mentor_profiles` only. The expand step for D88 added the column to
  `session_type_booking_configs` and dual-wrote it from the loader — and not from
  `profile_writer`, which serves the endpoint that is a mentor's only control
  over the setting.

  Harmless while `mentor_profiles` was still authoritative, and silent data loss
  the moment the readers moved below: a 200 that changes nothing anybody reads.
  The write now reaches **every one of that mentor's live offerings**, which
  reproduces the previous behaviour exactly — one setting for one mentor, stored
  per offering. Per-offering control needs an offering-management endpoint, and
  this fan-out is what it replaces.

- **The enum-to-text handoff plan was wrong in four places, one of which would
  have broken production.** Re-censused against the live schema before starting;
  the counts held, the hazards did not.

  `docs/handoff-enum-to-text-check.md` recorded *"no trigger reads an enum
  column"*. `trg_apply_mentor_status` reads `mentor_status_events.status_type`
  and writes `mentor_profiles.approval_status` and `.listing_status`. A plpgsql
  body carries no dependency records, so the planned `DROP TYPE` would have
  succeeded with every gate green and killed the mentor approval write path at
  the next insert. Those three columns are now one step, with the function
  rewritten in the same migration.

  The plan's five steps also covered only 15 of 21 columns — `mentor_profiles`,
  `session_events` and `session_participants` were named nowhere — and it
  undercounted the enum-column indexes (five, not three) and omitted
  `session_type_booking_configs.meeting_venue` from a default list its own prose
  numbered at eleven. The order is now eight steps and the arithmetic is shown.

  Settled decision #100 carried the same error, describing
  `mentor_status_events.reason` as *"the whole pattern by accident"*. It is not:
  the column takes free text on a decline and free admin text on an unlisting via
  `set_listing`, so no `CHECK` can guard it, and `UnlistedReason` is a set of
  sentinels rather than a closed vocabulary. Both the decision and the model
  comment now say so.

### Removed

- **The orphaned `unlisted_reason` PostgreSQL type — step 1 of 8 under settled
  decision #100.** It was attached to no column: `mentor_profiles.unlisted_reason`
  was replaced by `mentor_status_events` in `f2a8c31b7e45`, and dropping a column
  does not drop a type. No column changes type in this migration, which is the
  point — the conversion runs to eight migrations across 21 columns, and this one
  proves the harness before any data moves.

  The vocabulary is untouched. `UnlistedReason` is still written by `pause` and
  `decide` and read back by `may_self_resume`; only the unused database type goes.

  `infra/db/types.py` now carries three registries rather than one, and
  `test_every_domain_enum_is_registered_exactly_once` asserts they partition
  `domain/enums.py` — every vocabulary in exactly one of `PG_ENUM_TYPES`,
  `TEXT_CHECK_ENUMS` or `UNCONSTRAINED_ENUMS`. Disjointness is asserted as loudly
  as completeness, because a class left in two registries is precisely the
  half-converted state where the database and the models disagree.

### Changed

- **Settled decision #100 is complete: this schema has no PostgreSQL enum types
  left.** Step 8 converts `sessions.status`, `session_events.from_status` and
  `.to_status`, and drops `session_status` — the last of seventeen types across
  twenty-one columns, over eight migrations.

  **Four objects named the type**, including the `EXCLUDE USING gist` constraint
  that makes a mentor's overlapping live sessions impossible. It is dropped and
  recreated **inside one transaction and deliberately not `CONCURRENTLY`**: the
  `ALTER TABLE` holds `ACCESS EXCLUSIVE` throughout, so there is no window in
  which the constraint is absent — only a lock. The usual advice about avoiding
  long exclusive locks is right and does not apply here; a booking that blocks
  for a moment is correct, and one that races past an absent constraint is not.
  `lock_timeout` and `statement_timeout` are set, as `d7c31f8a2b45` set them.

  The guarantee was re-proved against real rows rather than inferred from the
  constraint definition: an overlapping `confirmed` session is refused, the same
  overlap as `cancelled` is accepted because it sits outside `LIVE_STATUSES`, a
  non-overlapping `confirmed` session is accepted, and an unknown status is
  refused by the new `CHECK`.

  **`pg_enum` and `PG_ENUM_TYPES` are deleted rather than left unused.** A helper
  that still existed would be an invitation to use it, and #100 would go back to
  being enforced by prose — which this project has watched fail twice with every
  gate green. `test_every_enum_type_matches_its_python_class` is replaced by
  `test_no_postgresql_enum_type_survives`, which fails the moment a migration
  creates a type. That is the live regression: the deferred `calendar_connections`
  and `search_impressions_suppressed` tables both declare enums in the canonical
  DDL, and building either verbatim would reintroduce one.

  The predicate test now also walks `ExcludeConstraint`, which lives in
  `table.constraints` rather than `table.indexes` and was therefore outside its
  reach. Four behavioural tests already covered that constraint, so this is
  defence in depth rather than a closed hole.

- **`session_events.actor_type` and `.reason_code` are now `text` + `CHECK` —
  step 7 of 8.** Neither has an index dependency: `ix_session_events_reason` is
  partial on `reason_code IS NOT NULL`, which names no enum literal, so it
  rebuilds with the table.

  **`from_status` and `to_status` are deliberately left alone.** Both are
  `session_status`, which is step 8. Converting one column of a shared type and
  leaving its siblings is the half-finished state the ordering exists to avoid,
  and it would leave `sessions.status` disagreeing with the two event columns
  about its own type. Four enum columns on one table, split across two
  migrations, on purpose.

  `reason_code` stays nullable. Legacy supplied no coded cancellation field at
  all — `Session Cancel/Decline Message` is free text and became `reason_text` —
  so every migrated cancellation event carries a null code, which is why the
  index over it is partial.

  **`session_status` is now the only PostgreSQL enum type left in the schema.**

- **`session_participants.role` and `.attendance_status` are now `text` +
  `CHECK` — step 6 of 8.** The first **unique** partial index to move.

  `ix_session_participants_one_mentor` is `UNIQUE ... WHERE role = 'mentor'` and
  is the only thing enforcing one mentor per session — the invariant that catches
  drift between `sessions.mentor_id` and the participant rows. It is dropped and
  recreated under the same name, and the invariant was re-proved directly: a
  second `mentor` row on the same session is refused, a `mentee` row is accepted.

  A wrong predicate here would be worse than a missing index. Rebuilt as
  `WHERE role = 'mentee'` the index still exists, is still unique, still carries
  its name — and the rule silently stops applying to mentors. Confirmed by
  mutation that only `test_every_partial_index_predicate_survives_a_conversion`
  catches it; the migration tests pass.

  The two levels of "did not show up" stay two levels: `attendance_status` is per
  person, `sessions.status = 'no_show'` is per session, and a mentee-attended,
  mentor-absent session has two participant rows and exactly one session outcome.

- **`meeting_provider` is now `text` + `CHECK` — step 5 of 8.**
  `session_type_booking_configs.meeting_venue` and `sessions.meeting_provider`.
  No index predicate names it and no function reads it, so this is the plain
  multi-column case.

  `sessions.meeting_provider` is nullable and the constraint needs no special
  case: `NULL IN (...)` is unknown, and a `CHECK` rejects only what is *false*.
  A null still means "venue not decided yet".

  The point beyond the conversion: `CUSTOM` has nowhere to keep a URL — D88
  removed `mentor_profiles.custom_meeting_url` because nothing had written it —
  and one migrated offering sits on it, while `ZOOM` has no legacy source at all.
  Neither could be shed while this was a PostgreSQL enum. Dropping either is now
  one `CHECK` swap.

  **The migration now ends with `ANALYZE`, and that is a real fix rather than a
  test appeasement.** `ALTER COLUMN ... TYPE` rewrites the table and discards its
  statistics, and the planner will not choose a partial index it can no longer
  cost: `test_the_completed_count_uses_its_partial_index` caught the
  completed-count query falling from `ix_sessions_mentor_completed` to
  `ix_sessions_mentor_window`. Without it a deploy serves worse plans until
  autovacuum catches up — in the window a migration is under most scrutiny.
  Steps 2 to 4 lack it because that planner assertion exists only for `sessions`;
  their tables are small enough that autovacuum closes the gap quickly.

- **The mentor status cluster is now `text` + `CHECK`, and `apply_mentor_status`
  was rewritten with it — step 4 of 8.** `mentor_profiles.approval_status`,
  `.listing_status` and `mentor_status_events.status_type` convert in one
  migration because the trigger reads one and writes the other two.

  **This is the step the whole re-census existed to find.** A plpgsql body
  carries no dependency records, so `DROP TYPE approval_status` succeeds while
  the function still names it: the migration applies, every migration test
  passes, `alembic check` reports no drift, and the trigger dies at the next
  insert with *type "approval_status" does not exist* — on the path every
  approval and every unlisting goes through. Confirmed by mutation: removing the
  rewrite reproduces exactly that error.

  The rewrite is a simplification, which is the tell the grouping is right. The
  old body carried `NEW.status_type::text::approval_status`, a double cast that
  existed only because PostgreSQL refuses a direct cast between two enum types.
  With all three columns `text` the cast has nothing left to do, and the hack
  disappears with the types that forced it.

  `mentor_status_events.status_type` also gains a `CHECK` it never effectively
  had: it is the value the trigger branches on, so an unknown one would reach the
  `ELSE` arm and be projected into `listing_status` — a silent mis-write rather
  than an error.

- **`lookup_status` is now `text` + `CHECK` — step 3 of 8, and the first shared
  vocabulary.** `institutions.status` and `scholarship_programs.status`. A
  `CHECK` cannot span tables, so each column carries its own and `LookupStatus`
  maps to a *set* of constraint names — the one real cost of `text` + `CHECK`
  over a shared enum type, paid deliberately for a droppable vocabulary.

  Also the first step to move index predicates. `ix_institutions_pending` and
  `ix_scholarship_programs_pending` are partial on
  `status = 'pending_review'`, which cannot survive the column changing type;
  both are dropped and recreated under the same name. Their definitions are
  deliberately **not** byte-identical afterwards — the literal is now
  `'pending_review'::text` — and the rows matched are unchanged.

  **`alembic check` sees a missing index but not a wrong one.** Measured rather
  than assumed: removing the recreation failed two tests, while recreating with
  a wrong predicate passed everything. `compare_metadata` does not diff `WHERE`
  clauses. A new test compares the literal values in every partial index
  predicate against what the model declares, which is what steps 6 and 8 need —
  an index rebuilt as `WHERE status = 'completed'` still exists and still has its
  name, and simply never matches the rows it was built for.

- **Booking notice is 24 hours, restoring a platform rule the migration was not
  carrying.** `session_type_booking_configs.min_notice_minutes` defaulted to
  **120** — two hours — and nothing has ever written it: the ETL does not set the
  column and no endpoint could. So every migrated offering sat on that default
  and would have permitted booking two hours out against a legacy rule of
  twenty-four, which allowed no same-day booking at all.

  Nobody chose that. It was invisible because 120 is a valid value: every count
  reconciled, every test passed, and `/slots` simply offered times legacy would
  never have shown.

  **This is user-visible in two ways.** `GET /users/{id}/availability/slots`
  stops offering anything within a day of now for migrated mentors. And
  `min_notice_minutes` — exposed by both `GET /users/{id}/session-types` and
  `GET /me/session-types` — reports **1440** where it reported 120. Same field,
  same type, different number.

  The column's new `CHECK` is **sanity, not policy**: `BETWEEN 0 AND 43200`. It
  refuses a negative and refuses beyond thirty days, and deliberately does not
  encode the 24-hour floor. The product rule — 24h minimum, 72h maximum, the
  mentor's choice per session type — is enforced at the Pydantic boundary and
  lands with the write schema, so that moving the range to `booking_policies`
  later is a config change rather than a migration. See settled decision #104.
- **Seven vocabularies are now `text` + `CHECK` — step 2 of 8 under settled
  decision #100.** `users.primary_role`, `admin_users.admin_role`,
  `auth_identities.provider`, `user_languages.proficiency`,
  `legal_documents.type`, `user_awards.verification_status` and
  `availability_exceptions.type`. Storage only: every value, default and index
  is unchanged, and the three indexes over these columns were verified
  byte-identical before and after.

  **The ORM keeps handing back `StrEnum` members**, via a new `str_enum()` in
  `infra/db/types.py`. This is not a convenience. `pg_enum` was doing two jobs
  and only one was obvious — the dialect-level `ENUM` also coerces rows back
  into the Python class, which plain `Text` does not. `Mapped[ApprovalStatus]`
  over `Text` type-checks clean and returns a `str`, `==` still works because a
  `StrEnum` member equals its value, and so the defect hides. `is` does not:
  `may_self_resume` compares identity, and converted naively it returns `False`
  for every mentor, leaving a self-paused mentor unable to resume. Fails closed,
  so not an exposure — a feature that stops working with a green suite.

  Two new parity tests assert the outcome for every converted column rather than
  trusting the migration that wrote it: one that a `CHECK` exists naming exactly
  the `StrEnum`'s values, one that the database and the model agree on which
  vocabularies have converted. Steps 3 to 8 inherit both. The `CHECK` is the
  only control on the ETL path, which writes these columns with hand-written SQL
  and never constructs a model.

- **D88 is complete: `mentor_profiles` loses `default_meeting_venue`,
  `requires_booking_confirmation` and `custom_meeting_url`.** The contract step,
  and the third of three releases — add and backfill, switch readers, drop.

  Every reader moved in the previous release, so **this changes no response**. A
  fresh migrate-then-load reproduces every resolved venue and confirmation
  exactly, which is the check that matters: the values are what moved, not the
  shape.

  **The real work was in the ETL, not the drop.** The two settings used to reach
  a booking config by being written to `mentor_profiles` by `load_profiles.py`
  and selected back out by `load_sessions.py` — two processes coupled through
  columns that no longer exist. They now travel on `SessionTypeRow` through one
  transform, with `transform/profiles.booking_defaults` the single place the
  legacy fields are read.

  `custom_meeting_url` was **removed rather than moved**: nothing in `src/` had
  ever written it and it was null on every migrated row. `MeetingProvider.CUSTOM`
  therefore has nowhere to keep a URL, and one migrated offering is on it —
  booking has to decide whether a custom venue needs a link. The value cannot be
  dropped from the enum until the `text` + `CHECK` conversion, because PostgreSQL
  has no `ALTER TYPE ... DROP VALUE`.

  **D88's fallback now has no members.** Every column on
  `session_type_booking_configs` is `NOT NULL`, so nothing inherits from a
  primary offering. `primary_session_type_id` is not redundant — it still names
  the offering a mentee lands on and drives display order — but it is no longer a
  source of values. Four settled decisions said otherwise and are corrected.

- **The session loader reports a mentor whose record it could not find.**
  `SessionPlan.booking_defaulted` lists mentors whose offerings took column
  defaults instead of their real venue and confirmation, and a non-empty list
  makes the run exit unresolved.

  Added because the failure it names was silent. Reading the wrong Bubble Thing
  parses cleanly, matches no anchor, and produces a load where every count
  reconciles and every mentor is on `google_meet` — reconciliation counts rows,
  not values.

- **Session calendars move to EduFurther's own Google account (ADR 0012, still
  `Proposed`).** That record named two behaviours as load-bearing and untested
  and asked for them measured before calendar work began. `scripts/calendar_spike.py`
  measured them against real accounts; both came back yes, and a third thing
  nobody had asked about changed the decision.

  **The invitation's sender is the account whose token made the call.** A
  mentor-owned calendar puts the mentor's personal email address on every
  invitation a mentee receives — not a setting, a consequence of whose token
  writes. The calendar is now EduFurther's, with mentor and mentee as attendees.

  The session still reaches the mentor's primary calendar, by invitation rather
  than by write, which is the placement ADR 0004 files under capabilities needing
  a sensitive scope. A mentor's consent narrows to `calendar.freebusy` alone. And
  **booking no longer depends on calendar connection at all** — the grant buys
  conflict detection, not the ability to hold a session.

  Three records disagreed about when `calendar_connections` ships and two were
  already falsified: `models/user.py` said M3, which shipped without it, and
  settled decision #80 said M4 on reasoning this measurement retired. Both now
  point at #21. ADR 0012 also gains instructions for whoever builds that table —
  the canonical DDL would create two PostgreSQL enum types that #100 forbids, and
  carries a Composio column that point 6 made dead.

- **A mentor's booking settings are read from their primary offering (D88),
  reader step.** `GET /users/{id}/session-types` takes each offering's
  `meeting_venue` from the offering itself, and the owner's mentor profile takes
  both `default_meeting_venue` and `requires_booking_confirmation` from the
  primary offering.

  **`default_meeting_venue` and `requires_booking_confirmation` on the mentor
  profile response are now nullable**, and are null for a mentor with no primary
  offering. That is not a degraded read: after the move these settings exist only
  on an offering, so a mentor who has claimed none has no value rather than a
  default one. The public `meeting_venue` stays required and is unaffected.

- **`session_type_booking_configs.meeting_venue` is `NOT NULL` with a server
  default, ending the venue fallback rather than re-pointing it (D102).**

  D88's fallback — *an offering without its own X uses the primary offering's X*
  — and the shipped contract that makes `meeting_venue` a **required** response
  field could not both survive the contract step. The chain's terminus was
  `mentor_profiles.default_meeting_venue`, which that step drops, and
  `trg_refuse_retiring_a_primary_offering` makes *release the pointer, then
  retire* the sanctioned two-step — so **live offerings with no primary is a
  state the guard creates by design**, and resolving through it produced a null
  the response model forbids.

  Every offering carries its own venue instead. The expand step's backfill had
  already written a real one onto every row, so this changes no resolved value;
  the migration's own backfill is defensive. `requires_booking_confirmation`
  keeps the fallback D88 describes.

- **Two fields left the public payloads because nothing renders them.**
  `years_of_experience` is gone from the discovery card and the public profile,
  and `current_country` from the public profile.

  Neither has a legacy source — neither appears in the M2 transform or loader, so
  both are null on every migrated row — and neither is drawn on the card or the
  profile design.

  **They part company after that, and only one of them was merely unrendered.**
  `years_of_experience` is genuinely consumed: the **admin review queue** reads it
  when deciding on a pending mentor, and so does the mentor's own profile. It
  stays writable and stays on both of those reads; this removes it only from the
  two public shapes that never showed it.

  `current_country_id` had no reader at all — not the owner's profile, not the
  admin queue, not the public profile, and the ETL never populates it. It was a
  field a user could `PUT` and never `GET` back, which is worse than an absent one
  because it looks like it was recorded. **It leaves `UserProfileWrite` as well.**
  The column survives and is a candidate for removal alongside the enum
  conversion.

  Breaking, and done now deliberately: nothing consumes these payloads yet, so
  this is the cheapest moment the removal will ever be. Tests assert their
  absence, because a field nobody asserts the absence of is a field somebody
  re-adds.

- **`dev_token.py` accepted a blank signing secret and gave wrong advice about a
  deployed one.** Two guards, both found by using the script rather than by any
  gate.

  The emptiness check asked `is None`, and `SUPABASE_JWT_SECRET=` parses to
  `SecretStr('')` — not None — so the script would mint a token signed with an
  empty key while the application refused to start at all. It now validates on
  the stripped value and **returns the raw one**: the application resolves the
  same setting through its own code and does not trim, so trimming here would
  sign with `abc` against a verifier holding `" abc "` and fail every token with
  a bare 401. `engine.py` already treated a set-but-empty `DATABASE_URL` this
  way; this was the only place in the codebase using identity rather than
  truthiness on a `SecretStr`.

  The JWKS refusal ended *"Unset it for local work"*, which is wrong in the case
  it is most often read: through `railway run` or any wrapper injecting a
  deployed environment, where it reads as an instruction to disable asymmetric
  verification in staging. It now says no locally signed token can be accepted
  while a JWKS URL is configured — the signing scheme, not a setting — and names
  Supabase sign-in as the way to get one.

### Added

- **`SessionStatus.WITHDRAWN` — a mentee taking back a request the mentor has
  not answered yet.** `SessionStatus` now has 8 members.

  **Not `CANCELLED`, and the distinction is the point.** A confirmed session
  called off is cancelled whoever calls it off, because the mentor has already
  committed time. Collapsing the two would put a request nobody accepted into
  the same bucket as a booking broken after agreement, and they carry different
  policy for refunds and for mentor-reliability statistics. The transition rule
  is `PENDING_MENTOR_APPROVAL -> WITHDRAWN` only; nothing enforces transitions in
  the schema, so it lives at the endpoint that writes the status and is recorded
  on the enum member.

  **`LIVE_STATUSES` is untouched**, which is the load-bearing part: a withdrawn
  request holds no booking slot. Verified against real rows — a mentor *can* be
  booked over a withdrawn request, because the double-booking constraint ignores
  it. Adding `withdrawn` to that predicate would keep a withdrawn request
  blocking the slot it just released.

  **Three `CHECK` swaps, and reversible.** Before the conversion this would have
  been `ALTER TYPE ... ADD VALUE` — one line and permanent. The downgrade
  narrows the vocabulary again and **fails loudly if any row already holds the
  value**, which is correct: reversing is only safe while nothing uses it, and a
  downgrade that silently rewrote live rows would be destroying a fact. Both
  paths verified.

  This is the first evidence #100 bought something rather than only costing eight
  releases. Its sub-rule — *do not add a value until something writes it* — was
  justified by permanence and does not survive its removal; `withdrawn` lands
  ahead of the withdraw flow deliberately.

- **`GET /api/v1/me/session-types` — a mentor's own offerings, including the
  ones they have switched off.** The SESSIONS management screen had no data
  source: `GET /users/{user_id}/session-types` looks like it serves it and
  cannot. That route takes no token and filters through `session_type_is_live()`
  plus `mentor_is_public()`, so it returns only *active* offerings of an
  *approved and listed* mentor — a mentor cannot see their own paused offering
  through it, and while unlisted or awaiting review it answers `404` to them as
  well as to everybody else. The new endpoint consults neither, and adds
  `is_active`, `category` and `application_stage`. The public contract gains
  nothing; a test asserts both key sets.

  **`session_type_is_live()` was recomposed rather than given an
  `include_inactive` flag.** That predicate decides what is *bookable* and is
  spread by `slot_store` and `profile_writer` as well, so a mis-defaulted flag
  reaching either would make a deactivated offering bookable again — silently,
  one keyword away, against #90's rule that switched-off means invisible **and**
  unbookable. The ownership and soft-delete pair is now `session_type_of()`, and
  `session_type_is_live()` is that plus the active check. Behaviour is unchanged
  for all four existing callers, and a predicate taking no flag cannot carry the
  mistake. Read-only: no migration and no schema change.

  **The ordering was correct by accident and is now correct by construction.** A
  mutation deleting `ORDER BY name` from the new query left the whole suite
  green: the partial unique index `(mentor_user_id, name) WHERE deleted_at IS
  NULL` covers this query's `WHERE` exactly, so an index scan returns rows in
  name order for free. That is a property of the *plan*, and a plan changes with
  row counts — at a few hundred offerings the free ordering disappears and the
  screen starts shuffling between refreshes. A test now disables index scans and
  asserts the order survives, which is what makes the clause load-bearing rather
  than decorative.

- **A mentor has a primary offering (D88), expand step only.**
  `mentor_profiles.primary_session_type_id` names the offering a mentee lands on
  and that unconfigured offerings fall back to; `session_type_booking_configs`
  gains `requires_booking_confirmation` beside the `meeting_venue` it already had.

  **Nothing is dropped and no reader moves in this release.** A column move is
  expand/contract, so this is one of three: add and backfill and dual-write here,
  switch readers next, drop the old columns last. Doing it in one would leave a
  rolling deploy with old code reading columns the migration had already emptied.

  **The backfill is not optional, and that was measured rather than assumed.**
  All five configs in the real export have `meeting_venue = NULL`, meaning
  *inherit* — so after the move they would inherit from the primary config, which
  is one of those same nulls, and the chain would have no bottom.
  `SessionTypeRead.meeting_venue` is a **required** field precisely because
  today's terminus cannot be null (D92), so an unbackfilled move would break a
  shipped response for every mentor. The ETL writes both locations for the same
  reason: the migration's backfill runs once, and a fresh migrate-then-load would
  otherwise get column defaults instead of the mentor's choice. Both facts are
  structural rather than counts off the dev export — that data is junk-filled, so
  its shape survives and its values do not, and the shape here is that the ETL
  writes a config with a duration and nothing else.

  `primary_session_type_id` is nullable because `mentor_profiles` and
  `session_types` reference each other: the row order is profile, offering,
  pointer, and a `NOT NULL` column could never be inserted. Null is also the
  ordinary state, structurally: a profile is created by the M2 load and offerings
  arrive with M4, so a mentor the sessions ETL never sees keeps a null pointer
  permanently.

  The backfill picks a mentor's earliest live offering by `id`. `uuid_generate_v7`
  is time-ordered only to the resolution of its timestamp and counter — rows
  written in one statement land in the same tick and the rest is random, measured
  rather than assumed — so the choice is *deterministic* on re-run and arbitrary
  among same-tick siblings. Acceptable because any live offering is a valid
  default and the mentor replaces it deliberately; it would not be if the choice
  carried meaning.

  `custom_meeting_url` is deliberately **not** moved. Nothing writes it and
  nothing reads it, so it belongs in the contract step as a removal rather than
  being relocated. Noted while in view: its CHECK runs one way only —
  `custom_meeting_url IS NULL OR default_meeting_venue = 'custom'` — so it permits
  a custom venue with **no** URL, leaving a mentor bookable with nowhere to hold
  the session. A fact about the constraint rather than about anyone's data.
- **`trg_refuse_retiring_a_primary_offering`** — the first business-rule trigger
  in this schema, and the mechanism D90 names.

  `ON DELETE RESTRICT` cannot do this job: nothing hard-deletes a session type,
  because retirement is `deleted_at` or `is_active = false` and both are UPDATEs,
  which no foreign key sees. The trigger refuses either while a mentor's pointer
  is on that offering, and permits both once the pointer moves.

  Retiring the offering mentees land on is now an explicit two-step: release the
  pointer, then retire. A trigger that silently nulled the pointer instead would
  leave a mentor with live offerings and no fallback source, which is the drift
  the guard exists to prevent. One existing test changed for this reason and says
  so.

  It is **not** `trg_set_updated_at` and must never join the list
  `timestamps_from_source` disables during a load — that helper names its trigger
  specifically, so this is safe today and would stop being safe if anybody
  generalised it.

- **The discovery card says where a mentor is from.** `origin_country` on
  `GET /mentors`, populated on 16 of 19 migrated profiles. The search document
  has always indexed it, so a mentee could *find* a mentor by their origin and
  then be shown a card that could not say it — a field good enough to match on
  and not to display.

- **The mentor profile reports what a mentor has actually done** — sessions
  completed, mentoring minutes, distinct mentees, and their own attendance rate.
  Derived every request (D56); the migration package drops the same two figures
  from *Mentor (front search)* as "DERIVED at query time".

  **One definition of "delivered", two readers.** The discovery card already
  showed a completed count and the profile shows the same number, so the
  predicate moved to `session_stats.delivered()` and both import it. A test pins
  the two to the same value.

  `attendance_rate` is the mentor's own — attended over expected — and is
  **`null`, never `0`**, when nothing is known: zero says "never shows up", null
  says "no data yet", and a new mentor must not be branded the first. Two states
  mean *unknown* and enter neither half: `pending` attendance, whose own
  docstring calls it "a real answer distinguishable from `NO_SHOW`", and a
  session with no mentor participant row at all — the state of two of the 105 dev
  bookings, which have no tracker. The denominator is terminal sessions only:
  a cancelled session is not a missed one, and a confirmed one next week has not
  happened.

  `mentoring_minutes` is **scheduled** duration. Daily's REST API exposes
  per-participant `join_time` and `duration`, so measured time is reachable
  there; Google Meet's conference records need domain-wide delegation on the
  organiser's Workspace, which a platform never holds for individual mentors, and
  ADR 0012 requests no scope that would reach them. One number meaning measured
  minutes on one venue and scheduled minutes on another would be worse than one
  honest definition.

  Measured rather than assumed: the completed aggregates use
  `ix_sessions_mentor_completed`, and the attendance query — whose
  `IN (completed, no_show)` predicate that partial index cannot serve — still
  reaches `ix_sessions_mentor_window` for the mentor scope. **No new index.**

- **The public mentor profile carries education, scholarships and languages.**
  `GET /mentors/{handle}` gains three lists, read by the **same** store functions
  that serve the owner-facing endpoints — narrowed at the schema, never
  re-queried, so the two cannot drift (#94).

  **The narrowing is the point.** `list_awards` returns `evidence_url`, a link to
  the holder's proof document, because the owner needs it; this endpoint takes no
  token. It also returns `verification_status`, which is `unverified` on every
  row since nothing verifies an award — publishing that would say something
  untrue about the holder rather than something true about the platform. Both are
  excluded, along with `degree_category`, `study_program`, `degree_level_slug`
  and `is_most_recent`, and a test asserts the exact key set of each list rather
  than searching the body for names. The first version did search, and reported
  `study_program` as leaked when it had matched inside `primary_study_program`.

  Education renders the degree exactly as the discovery card does —
  `COALESCE(degree_abbreviation, short_name)` — because a page showing "Ph.D" on
  the card and "Doctorate (PhD)" in the list below is one product with two
  spellings of one fact. A test pins the two to the same string.

  Six statements per profile, stated as a decision: three separate tables with
  three scopes cannot be one join without a fan-out to unpick in Python, and a
  profile is read once per page rather than once per row.
- **`list_education` stops ordering by `is_most_recent`.** The column is blank on
  every migrated row (D98), so the sort key decided nothing and read as though it
  did — and would have silently reordered every list the day anybody set it.
  Ordering is now `date_end`, then `date_start`, then `id`, nulls last, which is
  total.
- **`list_languages` is new**, and returns no `proficiency`. The column is
  `NOT NULL` with a `'fluent'` default that the ETL never overrides, so every
  migrated row claims a fluency nobody was asked about. Ordering is alphabetical
  rather than "primary first": `is_primary` is `false` on every migrated row for
  the same reason, and that is the third uniformly-valued flag in this schema
  whose ordering would have been a no-op.

- **The mentor card carries what a card actually renders**: the academic line —
  `Ph.D, Mathematics, Washington University` — and a completed-session count, on
  both browse and search. Four fields on `MentorSummaryRead`: `degree`,
  `study_course`, `institution`, `completed_sessions`.

  **Two of the three columns it reads were not the obvious ones**, and the dev
  export is what said so rather than the schema. `is_most_recent` looks like the
  way to pick which degree to show and is **blank on all 21 export rows**, so a
  card keyed on it renders nothing for anybody — legacy owned that flag, had
  write paths for it, and still never set it, which is what a value derived from
  *other rows* does. The entry is chosen at query time instead: highest degree
  level, then latest `date_end`, then id. Level outranks recency because a mentor
  taking a second bachelor's after a doctorate is still a doctor.

  And the field a mentee reads as "Mathematics" is `study_course` (21/21
  populated), not `study_program` (8/21, and holding degree *names* like
  `BSc (Bachelor of Science)`).

  A mentor with no education degrades to nulls and stays listed — excluding on a
  *display* field is a different thing from excluding on bookability.

  `completed_sessions` counts `status = 'completed'` only. `no_show` is its own
  status and stays out: a session the mentee never arrived at held the mentor's
  time and delivered nothing. Derived, never stored (**#56**) — the migration
  package lists `countCompletedSession` on *Mentor (front search)*, this exact
  card, and drops it as "DERIVED at query time". It is served by
  `ix_sessions_mentor_completed`, a partial index that had existed since the M4
  schema with no reader, and it runs *after* the page limit — 21 loops, not one
  per mentor.
- **`Education.shortForm` is migrated, one release before it would have been
  lost.** Populated on 21 of 21 export rows and read by nothing until now;
  after cutover the Bubble data is gone and the value cannot be re-derived. It
  lands on `education_entries.degree_abbreviation`, where null means *inherit*.

  Normalised on a strip-punctuation-and-casefold key, because the export spells
  one bachelor's degree as both `BSc` (11 rows) and `B.sc` (2), and one master's
  as both `M.Sc` (2) and `MSc` (1) — differences of dots *and* case, which a
  case-insensitive match alone would not fold. An abbreviation nobody listed is
  kept verbatim rather than folded to the nearest known value, because inventing
  a credential is worse than carrying an unusual one.
- **`degree_levels` gains `short_name` and `short_forms`**, both served by
  `GET /catalog/degree-levels` so a client picking an abbreviation gets the menu
  in the call it already makes.

  `short_name` is the generic fallback — `Ph.D`, `Master's`, `Bachelor's`,
  `Diploma` — and is **deliberately not a member** of `short_forms` for the two
  levels whose abbreviations vary by field. Guessing a specific form renders
  `B.Sc, Law` for a law graduate, which is precisely what the ISCED migration
  reshaped this table to prevent: *a Nigerian BSc, a UK BA and a US Bachelor's
  are the same level and three different words.* `short_forms` is advisory
  rather than a foreign key, since a user may hold something nobody listed.
- **`GET /mentors?q=` searches by name, school, programme, country and bio.**
  Postgres full-text search over a weighted document assembled from seven
  sources — `setweight` A for the name, B for headline and programme, C for
  schools and both countries, D for the bio — ranked with `ts_rank_cd`.

  **Two text search configurations, chosen per field.** `english` stems, which
  is right for prose and wrong for proper nouns: it reduces *Harding* to `hard`,
  so a search for "hard" would return that mentor. Four of the seven fields are
  names and take `simple`; the three prose fields take `english`, which is the
  choice this codebase already made for `about_me`. The query is parsed both ways
  and OR'd, since one parse would only ever reach half the document.

  **Browse keeps its keyset; search uses an offset**, both behind the one opaque
  token ADR 0016 made changeable for exactly this. A rank is neither in the row
  nor stable — "best match" grows to include quality signals that do not exist
  yet, and a token encoding today's ranking is invalidated the day the formula
  changes. Capped at 500 deep, because the systems built for search cap depth
  rather than solve it.

  Computed inline, so it is a sequential scan by construction — ~275ms at 500
  mentors and ~3.9s at 5,000, crossing D19's 200ms line somewhere between 500 and
  2,000. A range rather than a figure: the same unchanged statement measures 50ms
  or 285ms at 500 depending on machine load, so only back-to-back A/B deltas are
  comparable. The escalation is the same expression moved into a stored
  GIN-indexed column, returning the same rows in the same order.

  `ix_user_profiles_about_fts` — a GIN index built in M2 "deferred from M1" for a
  bio search that never shipped — stays unused. An expression index only serves a
  query whose expression matches it exactly, and a concatenated document does not.
  The stored column supersedes it, and that is the moment to drop it.
- **Mentor discovery — the endpoint that hands out the ids the others need.**
  `GET /mentors` pages every mentor a mentee could actually book, newest first,
  with no token. Three public reads existed before it and every one required an
  id or a slug you already had.

  **Bookable, never available.** A mentor appears while they are approved,
  listed, undeleted on both tables, and *set up*: an active offering that has a
  duration, and an active weekly availability window. It says nothing about
  *when* — availability is a computation over projected windows minus bookings
  and cannot be a `WHERE` clause, so filtering on it would mean computing slots
  for every candidate before paging and the cursor would stop being a keyset.
  Caching it in a column is the drift D20 rejected. A mentor booked solid for a
  month still appears, because they exist and take this kind of work.

  Ordered by `mentor_profiles.id`, not `users.id` — both are UUIDv7 and both are
  time-ordered, but they order different events, and a mentee of two years who
  starts mentoring this week is a new mentor.

  **No filters.** Service, school, degree and country are all reachable from
  existing tables and four of the indexes they want already exist; they arrive
  as query parameters, which is additive, where the sort order and row shape are
  not.
- **ADR 0016's base-case cursor exists at last.** The record says *"the id is
  the cursor when the display order is the id order"* — and only the amended
  two-part form had ever been written, because both earlier lists sort by a
  display name or a start time. `encode_id_cursor`/`decode_id_cursor` implement
  it rather than passing the id twice to the pair form, which works and reads as
  a mistake forever.
- **`offerings_for` replaces `list_service_offerings`**, taking several mentors
  at once. Per-mentor it was twenty round trips a page; a second batched function
  beside the single one would have been two queries of one rule, which is how the
  `is_active` filter ends up on only one of them.

- **A token for calling the local API by hand.** `scripts/dev_token.py --email
  ada@example.com` prints an access token the running application accepts, so an
  authenticated endpoint can be exercised without a Supabase round trip.
  `--header` prints a whole `Authorization` header; the token goes to stdout on
  its own, so it substitutes straight into a `curl`.

  **It runs against whatever `DATABASE_URL` is set and names the host it used.**
  There is no local-only restriction, because the binding constraint is the
  signing scheme rather than the database: an environment publishing a JWKS
  rejects this token whatever it was minted against. That is the one refusal —
  along with a missing secret, where nothing can be signed at all — and each
  replaces a bare 401 debugged in the application instead of the environment. A
  user who is unknown, soft-deleted or not yet provisioned is named, rather than
  handed a token that 404s at the route.

  The claim set moved to `app.infra.auth.dev_tokens`, which `tests/conftest.py`
  now calls as well, so the suite and the script cannot drift (#43). Settled
  decision #96 records why a minter shipping in `src/` is not a forgery tool.

- **A mentor's public profile, reachable by id or by legacy slug.**
  `GET /mentors/{handle}` returns who a mentor is, what kind of help they give,
  and what can actually be booked — with the session types **inlined** from the
  same function that serves `/users/{id}/session-types`, so the two can never
  disagree. No token: a mentee compares mentors before signing up.

  **D20's three-clause rule becomes one.** The package renders a profile if the
  mentor is listed, *or* the viewer has a session, *or* the viewer is an admin.
  The middle clause is dropped by product decision — a mentee with a session sees
  *that session*, which now carries the mentor's name — and the admin clause
  because admins read the owner-facing endpoint. What is left is exactly
  `mentor_is_public()`, so this endpoint writes no visibility predicate of its
  own.

  `users.slug` is the legacy public profile handle (#28), carried so existing
  links keep working. One statement resolves either handle: the segment is parsed
  as a UUID and falls back to a slug, so there is one query, one spread of the
  guard, and no branch that can carry a different set of clauses.
- **The public visibility predicate is a comparison again, not an `EXISTS`.** It
  was a subquery so a caller who forgot to join `users` could not silently lose
  the clause — sound, and incomplete. A correlated subquery hides
  `deleted_at IS NULL` from the planner, and `ix_users_slug_live` is **partial**
  on exactly that: measured at 20,000 mentors, a slug lookup was a sequential
  scan discarding 19,999 rows at 1.9ms, against 0.046ms once the predicate was
  direct. Forgetting the join now produces a cartesian product, which SQLAlchemy
  warns about and any row-count assertion fails on — loud enough, and it buys
  back every index on `users`.
- **The mentor's service offerings are read in one place.** The query was inline
  in `profile_store`; the public profile needed the same rows, and copying it
  would have been one rule in two places for the fifth time this milestone. The
  extraction also gained the `is_active` filter the inline version never had — no
  row is inactive today, so nothing changes now, but a retired offering will stop
  appearing on both profiles rather than one.
- **Sessions name the people in them.** `SessionRead` carried `mentor_id` and
  `mentee_id` as bare UUIDs, so a mentee's own session list could not say who the
  session was with — for **any** mentor, listed or paused. Each session now
  carries a `mentor` and a `mentee` object with `first_name`, `last_name` and
  `avatar_url` beside the ids, which stay: removing them would be the breaking
  change this avoids.

  **No soft-delete predicate on the party lookup, deliberately**, and the
  opposite of every public endpoint. There the mentor's lifecycle *is* the
  control; here the authorization is the session itself — a mentee had a real
  session with a real person, and their own history must not decay into a UUID
  because that person later left. A mutation adds the predicate and a test goes
  red, so a future sweep cannot quietly rewrite people's records.

  **`users` is joined inner and `user_profiles` outer.** `sessions.mentor_id` is
  `NOT NULL` with a foreign key, so the user row is guaranteed and an outer join
  there would be a guard nothing can reach. Nothing guarantees a profile row: a
  user who never filled one in has none, and an inner join would drop their
  sessions from **both** parties' lists.

  Every name is nullable because the columns are — the M2 transform maps them
  from optional Bubble fields — so a party with no name returns nulls rather than
  a server-invented placeholder no client could change. Measured: the four joins
  leave the keyset page still ordered by `ix_sessions_starts_at`, 0.14ms to
  0.55ms at 20,000 sessions.
- **A mentor's session types, publicly — the endpoint that makes slots usable.**
  `GET /users/{id}/session-types` returns everything a mentor currently offers,
  with the duration and notice that govern each. Take an `id` from it and pass
  it to `/availability/slots`. Until this shipped, slots required a
  `session_type_id` that **nothing handed out**, so the endpoint was correct and
  unreachable from a browse page.

  **Session type, not "offering" and not "service".** `service_offerings` is the
  closed six-row taxonomy already public at `/catalog/service-offerings`, and it
  is the axis matching joins on; a session type is one mentor's own bookable
  product. Using either word here would put two meanings of one term in the same
  API, which is the conflation settled decision #53 exists to prevent. Both are
  now pinned in the domain vocabulary.

  **`meeting_venue` is resolved, not returned raw.** Null on a config means
  *inherit from the mentor* (D21), so handing the null out would make every
  client implement the cascade and a client that got it wrong would show "no
  venue" for a mentor who has one.

  The response is an allowlist. `category` and `application_stage` are free text
  with no constraint, no vocabulary and no value in any row today, so publishing
  them would commit a public contract to a shape nobody has designed.
  `custom_meeting_url` is a **static room link** — a bearer credential anyone
  holding it can walk into — and a test asserts it appears nowhere in the body.
- **`session_types` leaves `EXEMPT_UNTIL_READ` and gets a real soft-delete case.**
  M4's schema pull request added the table with no reader anywhere, so its
  `deleted_at IS NULL` predicate had nothing to guard and the exemption said so
  in writing: *"the test below fails the moment one appears in `infra/db`."* It
  did — on the full gate, naming both halves of the fix. The set is now empty
  rather than removed, because the next table to arrive before its reader
  belongs in it.
- **Two soft deletes guard a public mentor, not one.** A mentor is a `users`
  row and a `mentor_profiles` row, each with its own `deleted_at`, and nothing
  ties them — deleting either leaves the other approved, listed and undeleted.
  Both public endpoints published mentors who had been removed. The user's
  half is an `EXISTS` rather than a join comparison, so it is correct wherever
  the predicate is spread and cannot be defeated by a caller who forgot to
  join `users`.
- **The public visibility rule now lives in exactly one place.**
  `infra/db/public_visibility.py` holds the predicates both public endpoints
  scope by, and the mutation batch proves it: dropping either half of
  `approved AND listed` turns **both** suites red. Two copies of one control is
  the shape this repository has already shipped once, in `list_session_events`,
  where each copy made the other untestable.
- **Bookable slots — the first public endpoint, and the first read with no
  viewer.** `GET /users/{id}/availability/slots?session_type_id=…&start=…&end=…`
  returns when a mentor could actually take a session of that type: declared
  availability, minus everything already booked, minus anything inside the
  offering's notice window, sliced into spans of its duration. No token is
  required — a mentee compares mentors before signing up.

  **`start` and `end` are optional**, so a browse page asks
  `?session_type_id=…` and nothing else. `start` defaults to **the mentor's**
  today and `end` to a week after it. It cannot default to the caller's today:
  there is no token, so no profile and no timezone, and IP or `Accept-Language`
  guess wrong for anyone travelling. Resolving in UTC instead would look
  equivalent and silently lose slots — a New York mentor at 02:00 UTC is at
  21:00 the previous day, and their evening is still ahead of them.

  `session_type_id` stays **required**. A slot's length and notice window come
  from the offering, so a slot without one is undefined; and falling back to
  "the mentor's only offering" would break every caller that omitted it on the
  day a mentor adds a second.

  What stands in place of a viewer is the mentor's own state, and it is **both**
  halves: `approval_status = 'approved' AND listing_status = 'listed'`.
  `apply_mentor_status` writes one or the other and never both, deliberately, and
  no CHECK ties them — so a `pending` mentor who is `listed` is a legal row, and
  gating on listing alone would have published an unvetted mentor's calendar.

  **Every session is subtracted, whatever its status.** A cancelled session keeps
  its slot: the mentor cancelled because they were busy, and handing the time
  straight back would rebook them into it. Releasing it will be a deliberate flag
  on the session, not an inference from the status. A `no_show` needs no handling
  at all — its start has passed, and nothing before `now` is ever offered.

  **The mentor's window defines the grid and a booking never moves it.** A
  09:00-12:00 window at 45 minutes offers 09:00, 09:45, 10:30 and 11:15; a
  session ending at 09:20 removes what it covers and leaves the rest where they
  were, rather than opening a slot at 09:20. A *block* does re-grid, because a
  mentor blocking part of their day has redefined it — consuming a slot and
  redefining the window are different acts.

  A slot is not a reservation. Two people can see the same one, and the second
  booking is refused by `sessions_no_mentor_double_booking`. One integration test
  books every slot the endpoint offered and requires all of them to commit, which
  proves the endpoint never offers time the database would refuse without either
  side asserting the other's arithmetic.
- **`ix_sessions_mentor_window`** — a non-partial gist index on `(mentor_id,
  session_window(starts_at, duration_minutes))`. The three existing per-party
  indexes are partial on the live statuses and on `completed` between them, so
  `cancelled`, `declined`, `expired` and `no_show` sat in none of them, and the
  slots read matched nothing: a sequential scan at 13.2ms against 20,000
  sessions, and 1.3ms with this. Built `CONCURRENTLY`, unlike the M1 indexes,
  because `sessions` already holds rows.
- **Reading sessions — three endpoints, all scoped by the people in the session.**
  `GET /users/{id}/sessions` pages a user's sessions newest first,
  `GET /sessions/{id}` reads one, and `GET /sessions/{id}/events` reads its
  history. "My sessions" means **either side**: a user may be a mentor and a
  mentee, so the list is `mentor_id = :viewer OR mentee_id = :viewer` rather than
  a required `?role=`. That parameter was nearly mandatory, on the belief the
  either-party predicate could not use an index; measured at 20,000 rows it uses
  `ix_sessions_starts_at` in 0.107 ms with no sequential scan, so the constraint
  did not exist. An admin may list a user's sessions — the same grant that reads
  their profile — but `GET /sessions/{id}` admits **parties only**, and a session
  you are not in is a `404` indistinguishable from one that does not exist.
  `session_events` carries no party column of its own, so it is reachable only
  through an ownership check on its session, which returns `None` rather than an
  empty list: `[]` would say "this session exists and has nothing in it", which
  leaks the session.
- **The M4 session tables** — `sessions`, `session_participants`,
  `session_events`, `session_types`, `session_reschedules`. A Bubble booking and
  its tracker are **one** `sessions` row, not two: the tracker records the same
  meeting from the mentor's side, and loading both would double every mentor's
  history. `session_events` is append-only and carries no `updated_at`, because a
  fact that can be edited is not a log.
- **A mentor cannot hold two overlapping live sessions** — `EXCLUDE USING gist`
  on `(mentor_id, session_window(starts_at, duration_minutes))`, partial on the
  live statuses. Legacy prevented double-booking with a check-then-insert, and
  **mostly in the frontend**, so it could be bypassed by any path that skipped
  that screen and could not see a second person clicking at the same moment. The
  package's expression could not be built — `timestamptz + interval` is `STABLE`
  and an index expression may not use one — so `session_window()` is a
  project-owned `IMMUTABLE` function, earned by measurement across five zones and
  three DST boundaries rather than asserted.
- **The M4 transform** — bookings and trackers merged, participants derived, and
  a `session_events` history reconstructed from `Modified Date`, which the export
  shows is the last state transition rather than an edit clock. Two events per
  session, never three. Trackers with no booking are quarantined rather than
  guessed at.
- **`load_sessions`** — the M4 loader, reconciling **inside** the transaction like
  M3's. 177 sessions, 350 participants, 246 events and 5 session types from the
  dev export, stable across a re-run.
- **Settings refuse to start on a mixed set of Supabase credentials.** `DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_JWKS_URL` each name a project, and nothing tied them together: point the DSN at staging while the Supabase values still name production and `provision_auth` reads users out of one project's database and creates real auth accounts in another's, reporting `created 43 … failed 0`. There is no bulk undo. Narrow deliberately — a value takes part only if a project ref can be read out of it, so a `localhost` DSN beside a real `SUPABASE_URL` stays legal, which is how everyone here develops.
- **`ENV_FILE` selects the dotenv to read**, so switching environment is one file rather than five variables edited by hand — which is the shape the mistake above actually takes. Works in PowerShell, where the `set -a; . file` idiom does not and `$env:VAR = ...` persists for the rest of the session.
- **`reconcile_availability`** — M3 was the only phase loading without one. It
  runs **inside** the transaction and raises, because reconciling after a commit
  reports a problem it is no longer able to undo.
- **Row counts deliberately do not reconcile one-to-one.** Every prior phase
  compared source rows to loaded rows; availability breaks that three ways at
  once — exceptions fan out 1:N, Gen A rules are quarantined on purpose, and
  overlapping windows merge so one anchor never reaches the table. Asserting
  `source == loaded` would fail every run, and the fix somebody would reach for
  is a loosened comparison that checks nothing. What is asserted instead is that
  every source rule was **accounted for**: loaded, quarantined, dropped, refused
  or absorbed. A row in none of those vanished with nobody deciding it should —
  the one failure no row count can show, because the loaded total is *meant* to
  be smaller here.
- **The plan's accounting is data, not prose.** `dropped` and `merged_overlaps`
  were formatted strings; reconciling against them meant parsing a Bubble id out
  of a sentence, and improving the wording would have silently stopped the
  accounting while the totals still balanced. They now carry anchors as fields,
  with the rendering in `report()` where it belongs.
- **`AvailabilityCounts` removed.** It reported the number of rows handed *in* —
  an assertion about the database made without asking it. What landed is read
  back by the reconciliation.

- **A mentor's availability endpoints** — list, add, change and remove recurring
  weekly windows and dated exceptions, under
  `/api/v1/users/{user_id}/availability`. Rules go out as **wall clock plus an
  IANA zone, never an instant**: a recurring rule has not named a date, and
  converting it would bake in one date's UTC offset — the bug that breaks twice
  a year.
- **Overlapping windows are a 409, not a 500.** The exclusion constraint reaches
  the API as an `IntegrityError`; unmapped, an ordinary mentor mistake — dragging
  a window across a neighbour — would have been a server error.
- **Reads are owner-and-admin only, deliberately narrower than D20.** That rule
  renders a profile if the mentor is listed, *or* the viewer has a session with
  them, *or* the viewer is an admin — and the middle clause has no table until
  M4. Shipping the two that exist would drop precisely the one protecting a
  mentee whose mentor has since paused, which is the case D20 was written for.
  Widening later is additive; narrowing after a client has built against it is
  not.
- **`normalise_timezone` is now shared** between the API boundary and the ETL.
  The columns are `text` with no CHECK — `pg_timezone_names` is not immutable, so
  PostgreSQL will not accept it in one — which makes this the only thing standing
  between a request and a value that raises inside the projection later.

- **The availability ETL** — `domain/transform/availability.py`,
  `infra/etl/availability.py` and `scripts/load_availability.py`. Against the
  dev export: 24 legacy rules become **11 loaded, 11 quarantined and 2 dropped**,
  and 2 exception rows fan out to **6**.
- **Legacy `CalendarSettings` has two generations, and they mean different
  things.** Rows carrying a `timeZone` treat the exported time as the declared
  wall clock; rows without one were displayed to the mentor in UTC, five hours
  away. The discriminator is the presence of that column, and applying either
  rule to both halves is a five-hour error on half the table — in both
  directions, one line apart.
- **Generation A is quarantined, never loaded.** No mentor in the dev export
  owns both an old rule and a booking, so nothing here can settle which reading
  is real; the 1,073 production bookings can. The report prints **both candidate
  readings side by side** so that decision is a comparison rather than a
  re-derivation, and it names the **4 mentors** who arrive at cutover with no
  availability and must re-declare it.
- **One `CalendarExtra` row becomes one exception per blocked date**, anchored
  `{bubble_id}:{iso_date}`. The dates are a comma-joined list in which each date
  itself contains a comma, so the project's own `normalise_list` would have
  split `Jan 13, 2025 12:00 am` into two fragments and produced dates nobody
  entered.
- **Overlapping windows are merged into their union before insert.** Not
  tidiness: the exclusion constraint means an unmerged pair aborts the load
  rather than landing badly. Dev has none across 6 multi-row weekdays; production
  is 192 rules against 24.

- **`domain/availability.py` — recurring availability projected onto real
  instants.** One pure function turns weekly wall-clock windows plus dated
  exceptions into UTC intervals over a date range. No I/O, no framework, no
  database row: `api` and `infra` map onto its value objects.
  **RFC 5545 §3.3.5 is the specification**, because it is what every calendar
  the mentor already uses implements — a local time occurring twice resolves to
  the first occurrence, one that never occurs is read with the offset before the
  gap. Python's `fold=0` is exactly that, and it is now normalised rather than
  inherited, because `datetime.combine` takes `fold` from the `time` it is given
  and a caller could otherwise bypass the spec silently.
  Both endpoints resolve independently, so a 09:00–17:00 window is seven real
  hours the day the clocks go forward and nine the day they go back. That is
  correct: the mentor's day genuinely is shorter. A window declared inside the
  spring-forward gap resolves to zero length and is dropped — the database
  `CHECK` runs on wall clock and cannot see it.
  Blocks are applied after overrides and therefore win, which is the settled
  precedence. Exceptions resolve in **their own** timezone, not the rule's, and
  are no longer clipped by calendar date: a whole-day New York block really does
  overlap a Kolkata window on the following date, and clipping made the answer
  depend on the caller's query window rather than on the data.

- **A mentor's availability windows may not overlap on one weekday** — a partial
  `EXCLUDE USING gist` over a `timerange` type PostgreSQL does not ship. Two
  overlapping windows carry no information their union does not, so the pair is
  a data-entry mistake rather than a state; and the mentor who later edits one
  copy changes their availability in a way the other silently undoes. In the
  schema rather than the write path because it is an invariant about data, true
  on every path — otherwise it gets written in the endpoint, again in the ETL,
  and again in the next bulk editor. Half-open, so 09:00–12:00 and 12:00–14:00
  still touch without colliding, and partial, so a switched-off or soft-deleted
  window stops blocking the slot it used to occupy. The repository's first
  exclusion constraint; M4's double-booking guardrail follows the same shape.

- **`availability_rules` and `availability_exceptions`** — the M3 schema. A
  mentor's recurring weekly windows are stored as wall-clock time plus an IANA
  zone, never as a pre-formatted local string: legacy `CalendarSettings` kept
  four such columns and in the dev export they disagree with the stored time by
  five hours on half the rows. Several rows per mentor per weekday, because
  split availability is real in the data and the legacy one-row-per-day shape
  could not hold it.
- **`btree_gist`**, so `ix_availability_exceptions_range` can be a GiST index
  over `(mentor_user_id, date_range)` — uuid has no GiST operator class without
  it. Confirmed present on the local container, the CI image and Supabase before
  the index depended on it.
- **A soft-delete exemption that expires by itself.** The two new tables carry
  `deleted_at` and nothing reads them until the availability endpoints ship, so
  they are exempt from the soft-delete sweep — and a test fails the moment any
  `infra/db` module names one of them, which turns "somebody will remember" into
  a red build.

- **`tests/e2e/` — a real uvicorn server**, and the first occupant of a directory
  that had held only a `.gitkeep`. Ten tests covering what an in-process
  transport cannot express: a chunked body over the ceiling, an honest
  `Content-Length` over it, a lying one, a client that disappears mid-body, and a
  real multipart upload. Requests are written as raw bytes, because an HTTP
  client normalises the very things under test.
- **A test pinning uvicorn's framing**, labelled as pinning a dependency rather
  than our code: `limits.py` argues in prose that a lying `Content-Length` is not
  a hole *because the server delivers only what was declared*, and nothing
  checked that until now.

- **`POST /api/v1/users/{id}/avatar` and `/banner`** — a user uploads their own
  profile picture or banner. JPEG, PNG or WebP, up to 5 MB. Owner only: an admin
  may read these profiles and may not write them.
- **Every image is decoded and re-encoded**, which is what removes camera
  metadata — a phone photo carries the GPS coordinates it was taken at, and a
  profile is public. ADR 0019 left that open and it is now closed by
  construction: there is no strip step to forget, because nothing hands the
  metadata to the encoder. `scripts/migrate_assets.py` goes through the same
  function, so there is one population of images rather than two.
- **Stored at one size per kind** — 512px for an avatar, 1500px for a banner,
  longest edge. Larger images are resized rather than refused; a smaller one is
  left alone rather than enlarged.
- **The image a user replaces is deleted**, after the profile points at the new
  one and never fatally. Paths are keyed on the user and a content hash, so
  nothing else can be pointing at it — and uploading the same image twice lands
  on the same path and deletes nothing.
- **`api/limits.py`** — a 6 MB ceiling on the request body for every route,
  refused with 413. It has to be middleware: FastAPI parses the multipart form
  and spools it to disk before the endpoint runs, so the same check written
  there limits nothing. Found by a mutation batch and confirmed by measuring the
  order with an 8 MB body.
  **The ceiling counts the bytes that arrive**, and does not simply read
  `Content-Length`. A chunked request carries no length, so a header-only check
  compared nothing and let 7 MB through — verified against a real uvicorn
  server. The declared length is still checked first, because it is the only one
  of the two that can refuse before a byte is read. On a JSON route the
  unbounded case was memory rather than disk, which is why this is not scoped to
  the upload endpoints.
- **Pillow**, the only dependency in the project that decodes an untrusted
  *file format*. A 50-megapixel ceiling is applied from the header, before any
  bitmap is allocated; Pillow's own limit only warns and decodes anyway.
- **`.github/dependabot.yml`** — `github-actions` only, monthly, **grouped into
  one pull request** with `open-pull-requests-limit: 1`. This repository merges
  one pull request at a time; a month with five action releases would otherwise
  open five competing for that lane. No `pip`/`uv` ecosystem: `uv.lock` is the
  source of truth and a bot editing it changes what the application runs, not
  what builds it.
- **Settled decisions 76-79** and eight `failure-modes.md` rows, four of them
  found by the mutation batch rather than by the suite and three by a review that
  probed a real server instead of the in-process transport.
- ADR 0002 (Bubble export strategy) and ADR 0003 (read-only freeze cutover).
- `project-conventions` filled in with the project's settled decisions, domain
  vocabulary, guardrails, and the current enforcement blind spots.
- A `references/failure-modes.md` row for the stacked-PR merge that closed a
  dependent pull request irrecoverably, and the merge order that prevents it.
- `main-guard.yml`, which fails when a commit reaches `main` without a pull
  request. It detects a bypass after the fact and cannot prevent one; server-side
  prevention needs GitHub Pro or a public repository (issue #9).
- ADR 0004 (calendar integration), 0005 (data platform) and 0006 (messaging
  build-vs-buy), plus `docs/adr/README.md` as the index, and settled decisions
  12–19 with the `port`, `ror_id` and `whatsapp_conversation_id` vocabulary.
- ADR 0007 (adopt the migration package as the target data model) and settled
  decision 20. `docs/edufurther-migration/` is committed as the canonical target
  schema — received, never edited here. It reconciles four points where the
  package and this repository disagreed: the migration anchor column is
  `legacy_bubble_id`, a booking and a session are one `sessions` row, the export
  transport is the Bubble Data API staged as raw `jsonb`, and the vendor WhatsApp
  thread identifier is renamed `whatsapp_conversation_id`. Institutions,
  first-login authentication and message-thread scope stay open and are deferred
  to ADRs 0008–0010. Adds a guardrail requiring credential fields to be redacted
  at extraction rather than at load, and records in `docs/adr/README.md` how a
  partially superseded record states its status.
- ADR 0011 (Alembic is the migration chain) and the persistence foundation:
  SQLAlchemy 2.0 with asyncpg, the async engine and session factory in
  `infra/db/`, the Alembic chain outside `src/`, a Postgres 17 compose service on
  port 55432, and database tests that skip locally without `TEST_DATABASE_URL`
  but fail in CI, where `REQUIRE_DB_TESTS=1` turns the skip into an error. The
  first migration installs `pgcrypto`, `uuid_generate_v7()` and `set_updated_at()`.
- `countries` (249 rows) and `languages` (7,078 rows), seeded from ISO 3166-1 and
  ISO 639-3 by `scripts/generate_reference_seeds.py`, which emits the migration
  rather than the migration being hand-written. Both are keyed on their natural
  ISO code. Languages use 639-3 because 639-1 omits Nigerian Pidgin entirely, and
  only 174 of the 7,078 carry a two-letter code at all.
- Settled decisions 21–25: phase-scoped enums and lookups with the foundation
  exception, natural keys on ISO lookups, the per-table `updated_at` trigger, no
  `legacy_bubble_id` on reference tables, and the Alembic chain.
- ADR 0009 (first-login authentication) — Supabase Auth for every login, a
  6-digit email code as the default with a sign-in link offered as a choice,
  delivered through a Send Email Hook that also routes authentication mail
  through Emailit. Drops `auth_codes`, `auth_code_purpose` and
  `users.password_hash` from the target schema, and makes `users.id` the Supabase
  auth user id.
- ADR 0008 (institutions) — the hipolabs registry, populated on demand and stored
  once referenced, with a surrogate `uuid` primary key and `domain` as the natural
  key. **No `ror_id` column**, which supersedes settled decision 17 in full and
  retires the `ror_id` vocabulary term — both **on acceptance**, along with
  `README.md`'s stack table. The record is `Proposed`, and the settled-decisions
  table is loaded at the start of every build, so it is not rewritten to describe
  a decision that has not been made. Same sequencing as ADR 0009.
  The record makes no deviation from the migration package, whose own D15
  had already been revised from seeding ROR to populating from hipolabs. It records
  two M2 prerequisites the M0 chain does not provide — the `pg_trgm` extension and
  the `lookup_status` enum — and that nothing monitors the hipolabs dependency the
  decision accepts.
- `fastapi[standard]` replaces the bare `fastapi` dependency, adding `jinja2`,
  `python-multipart`, `email-validator` and the `fastapi` CLI — 14 packages in
  total. The extra also pulls `fastapi-cloud-cli`, which depends on **`sentry-sdk`**,
  so an error-reporting SDK is now a runtime dependency that arrived without an
  ADR. It is listed in `[tool.check-layers.forbidden-external]` for `domain`,
  `api` and `core`, and **that entry is narrower than it sounds**: it covers those
  three layers *except the composition roots*. `main.py` and `api/deps.py` are
  exempt, and `main.py` is skipped before exemption is even consulted because it
  sits outside any layer — so the one file where `sentry_sdk.init(dsn=…)` would
  naturally be written is the one file the guard does not read. What makes this
  safe is not the denylist: `sentry_sdk` is inert without an `init()` call, nothing
  calls it, and no DSN is configured. The entry prevents an accidental import;
  initialising it deliberately would be an ADR. The guard was verified by importing
  `sentry_sdk` into `domain/` and then `core/` and watching `check_layers.py` fail
  on each before the probes were removed.
  `fastapi[standard-no-fastapi-cloud-cli]` is the upstream extra that excludes
  both, and was considered and declined: FastAPI Cloud is the deploy target and
  the CLI is used. The standing cost is that `pip-audit --strict` now covers 14
  more packages, so a future `sentry-sdk` advisory will break the weekly security
  workflow for a dependency the application never calls.
- ADR 0012 (Google OAuth scopes and client split) — both Google integrations stay
  inside the **non-sensitive** scope tier, and sign-in and calendar move to
  **separate Cloud projects**. Calendar uses `calendar.freebusy` to read
  availability and `calendar.app.created` to write events into a secondary
  calendar the application creates; the latter is a full create/change/delete
  capability and is non-sensitive, because the application can only ever touch a
  calendar it made. Sign-in keeps `openid`, `userinfo.email` and
  `userinfo.profile`. The projects are split because the consent screen — branding,
  scopes, verification status — is project-level, so a sensitive scope added for
  44 mentors would attach a user cap to 1,200 sign-in users. It supersedes ADR
  0004's "submit it for sensitive-scope verification" clause, and records that two
  behaviours it depends on are untested: whether an app-created calendar sends
  attendee invitations, and whether its busy intervals reach the mentor's own
  free/busy.
- **M2's four lookup catalogues** — `institutions`, `degree_levels`,
  `service_offerings` and `scholarship_programs` — with the `pg_trgm` extension
  and the `lookup_status` enum. Two are open, with `status` and `merged_into_id`
  for the curation queue ADR 0008 and package D15 require; two are
  closed vocabularies the product defines. `institutions` ships empty from the
  migration and is filled by the sync below, never by a seed. Only
  `lookup_status` is created — the other six enums in
  `02_profiles.sql` arrive with the tables that use them (decision #21). Country
  becomes `country_id uuid` rather than the package's `char(2)`, per ADR 0015, and
  the package's deferred `created_by`/`approved_by` attachments are ordinary
  inline foreign keys here because `users` already exists in our chain.
- **`service_offerings` seeded with six rows, where the package seeds none.**
  Reading the legacy option set corrected package D12's premise: Bubble held
  **one** vocabulary used by both sides, not two unmapped ones — both columns
  store the display name as text at selection time, so the mentee side is six
  parents and the mentor side five parents plus five children and renames.
  Seeding the parents is what makes matching work at all. Settled decision 53
  records why the table is closed and what re-opening it would cost.
- **`scholarship_programs` seeded with ten curated programmes**, so
  suggest-before-create has something to match against from day one; `funding_type`
  and `degree_levels` are left empty deliberately. Settled decisions 54 (model
  module layout and its split threshold) and 55 (the fail-open `status` default,
  and the obligation it puts on every write path until admin curation ships).
- Two `failure-modes.md` rows: a text snapshot of a controlled vocabulary is a log
  of past UI states rather than a vocabulary, and a migration rewritten under an
  unchanged revision id leaves every already-migrated database silently diverged.
- **M2's seven profile tables** — `mentor_profiles`, `mentor_service_offerings`,
  `education_entries`, `user_awards`, `mentee_goals`, `mentee_goal_countries` and
  `mentee_goal_needs` — with five enums, and the full-text index on
  `user_profiles.about_me` that M1 deferred. Six of the seven are reshaped for
  ADR 0015: a surrogate `id` with the invariant the package's key carried
  re-declared as `UNIQUE`. **Mentor-only and mentee-only tables reference
  `mentor_profiles(user_id)` and `mentee_goals(user_id)` rather than
  `users(id)`** — the same value, but the foreign key makes it structurally
  impossible to attach a mentor-only row to a mentee, and repointing it would be
  a one-word edit that changes nothing visible.
- **`user_scholarship_experience` and `scholarship_relationship` are not
  created**, and `user_awards` gains a nullable `scholarship_program_id` instead.
  The legacy field behind that table has no option set and no values on any row,
  so there was nothing to migrate — and it overlapped `user_awards`, giving "I
  won Chevening" two legal homes. Dropping it left `scholarship_programs` with no
  consumer anywhere in the package; the link restores one, on the
  `school_name_raw` + `institution_id` pattern where the raw text is always kept.
  Settled decision 59.
- **The package's `usage_count` column is not carried**, and the two curation
  queues are ranked by `created_at` with the usage figure computed at query time.
  It was briefly present and is removed before release, so the net effect is that
  it never shipped. The package declares it, indexes it and documents it as the
  queue's approve-or-merge signal while specifying **nothing that maintains it** —
  so it would have been zero on every row forever, and the index would have
  sorted a constant. Settled decision 56 states the rule the codebase had
  demonstrated twice and written down neither time, with a `failure-modes.md` row
  for how it got as far as a merge unnoticed. ADR 0008's open-questions section
  gains a dated correction note — the record is immutable and its six decisions
  are untouched, so the original text stays and the note says what changed, which
  is the convention `docs/adr/README.md` sets for a premise overtaken by events.
- Settled decisions 57 (`requires_booking_confirmation` defaults to `false`, and
  what bounds the exposure) and 58 (legacy `meetingDuration` is an M4 input,
  becoming the duration of each auto-created "General Mentorship" session type).
  `education_entries` ships without `school_short_form` — legacy `shortForm`
  holds degree abbreviations, not school ones — and without `field_of_interest`,
  which is deprecated in the source application.
- `add_user` moves from `test_identity_schema.py` to `conftest.py`, now that a
  second schema suite needs it. A private copy that supplied its own `id` would
  keep passing after somebody removed the `uuid_generate_v7()` default.
- **`test_no_declared_identifier_exceeds_the_postgresql_limit`**, after review
  found a foreign key whose convention-generated name was 65 characters and
  which PostgreSQL therefore held under a truncated, hashed name appearing
  nowhere in the repository. SQLAlchemy shortens silently — no warning, and
  `op.f()` does not exempt it — while `alembic check` compares foreign keys by
  column signature rather than by name, so the whole gate stayed green. The name
  is shortened and the guard walks `Base.metadata`. Three further names in this
  schema sit at 58 and 59 characters, so the margin was one long table name.
- **M2's profile transform and loader** — `domain/transform/profiles.py`,
  `infra/etl/profiles.py` and `scripts/load_profiles.py`, filling all seven
  profile tables from a legacy snapshot and reconciling them. `domain/transform`
  becomes a package (`identity` + `profiles`, everything re-exported so no import
  changed), and `infra/etl/cli.py` holds what two loader scripts now share.
  Verified against the dev export end to end: 12 mentor profiles, 21 education
  entries, 13 goals, 10 awards, run twice with identical counts and no
  `updated_at` touched by the importer.
- **Settled decision 60** — a migrated row is attributed by the **user-side
  link**, never by `Creator`; `Creator` is a cross-check whose disagreements are
  reported, and the sole path only for `Scholarship-Awards`, which has no
  user-side link at all. `Creator` was re-exported as a Bubble user id rather
  than an email, which makes the comparison exact and removes the ambiguity a
  duplicate address would cause.
- **Settled decision 61** — institution matching is a separate, re-runnable
  pass; `education_entries` loads with `institution_id` null. Measured against
  real school names, a genuine typo scores **0.773** and two different Nigerian
  federal universities score **0.750**, so no similarity threshold separates
  them: exact matches auto-link and the rest are suggestions for a human.
- `EXPORT_TIMEZONE` and the canonical record's key names move to
  `domain/bubble.py`. The first was written out in two scripts and pinned by a
  test comparing exactly those two — a third would not have been covered. The
  second cost a `NOT NULL` violation on the first real load, after `modified_at`
  was hand-typed as `"updated_at"` in four places, matching the column it feeds
  rather than the key it reads.
- Three `failure-modes.md` rows: a uniform dataset can agree unanimously with a
  broken implementation (every dev date is midnight, which hid a UTC conversion
  moving evening dates forward a day); a key shared by a producer and its
  consumers belongs in the layer that defines the contract; and every test
  importing `scripts` passed only because `alembic.ini`'s `prepend_sys_path`
  had left the repository root on `sys.path` — they failed run alone. Fixed with
  an explicit `pythonpath` in the pytest config, and the suite now passes with
  random ordering enabled rather than suppressed.
- Tests for what `load_profiles.py` **surfaces**, not only what it computes.
  Unattached rows, `Creator` disagreements and nulled award years were each
  covered by a transform test, and no transform test can tell whether a value
  ever reaches a screen — the same shape as a column that reads as operational
  and is implemented by nothing. `scripts/` is checked by ruff alone.
- **The institution catalogue is mirrored and refreshed weekly** —
  `domain/institutions.py`, `infra/clients/hipolabs.py`,
  `infra/etl/institutions.py`, `scripts/sync_institutions.py` and a
  `sync-institutions.yml` workflow on a Monday cron. The mirror upserts on
  `domain` and the link fills `education_entries.institution_id` in a separate,
  re-runnable pass (decision 61). Verified against the real catalogue: 10,257
  records, 10,250 stored, and 21 of 21 education entries linked; a second sync
  refreshed every `last_synced_at` and moved zero `updated_at`.
- **`institutions.last_synced_at`, and `institutions.country_id` becomes
  nullable.** `last_synced_at` is stamped on every row a sync saw, including
  unchanged ones, so `max(last_synced_at)` answers how stale the mirror is —
  which is the objection ADR 0008 raised against mirroring at all. `country_id`
  goes nullable so a user-created row can be completed by an admin at review,
  rather than the user being asked for a field the review process exists to
  supply.
- **ADR 0020 and settled decision 62** — the catalogue is fetched over **HTTPS**
  from the source repository and the hipolabs **HTTP API is never called**. Its
  API has no TLS, so a browser on an HTTPS page cannot reach it: 0008's
  client-side autocomplete is unbuildable rather than merely deferred, while the
  same data *is* served securely from the repository. Weekly is sized to the
  measured upstream cadence — 100 commits over 177 days, one change every ~2
  days. GitHub Actions holds the schedule because `migrate.yml` already reaches
  the database from a runner, and Actions moves with the repository rather than
  the host, so decision #13's Railway exit is untouched. **Staging only** — there
  is no production environment during the build and migration phase, and a
  schedule pointed at one that does not exist fails every week quietly enough
  that nobody looks.
- **A domain two records share is collapsed before the write, not by
  `ON CONFLICT` afterwards.** Absorbed by the conflict clause, the second record
  rewrote the first on *every* sync forever — `updated_at` moving on two rows
  whose stored content never changed, which is precisely what `last_synced_at`
  was added to say. The test that claimed to cover this asserted it for a domain
  appearing once, so it could not see the case that was broken.
- **The sync workflow fails closed.** Its exit-code branch named 1 and 2 and sent
  everything else to the implicit 0 of a false branch, so an OOM kill (137) or a
  missing interpreter (127) rendered as a green weekly check. Only 2 is now
  forgiven. This matters more than it looks: nothing alerts on a sync that has
  stopped, so the weekly check is the only signal there is.
- Two records that are skipped rather than guessed, both reported by count: an
  unresolvable country code (measured at 5 of 10,257, all `XK` — Kosovo, a
  user-assigned code outside ISO 3166-1) and a record with no domain (0 today).
  Neither is defaulted; a wrong country propagates into "who studied in the UK"
  and nothing would surface it. Two upstream domains carry two names each and
  collapse to one row, counted so the total is not read as a loss.
- **A fresh environment has an empty catalogue until the sync runs** — a
  deployment step, recorded in ADR 0020 rather than discovered by whoever
  provisions the next environment.
- **A name the catalogue carries twice is a question, not a match.** Upstream
  holds 73 exactly-duplicated names over 158 records, and they cross borders —
  `City University` is a university in the United States, in Bangladesh and in
  the United Kingdom. The first implementation kept whichever row a `SELECT`
  without `ORDER BY` returned last, so the link was a coin toss that could
  change between runs, and the country of study derives from it. Ambiguous names
  now report separately from unmatched ones, because a miss wants the
  institution added and an ambiguity wants somebody to say which.
- **A sync with nothing usable exits non-zero.** Renaming one upstream key
  refused every record, mirrored nothing, printed the refusals and exited **0** —
  including the dry run CI runs first, so the weekly check stayed green over a
  catalogue that had quietly stopped updating. `HipolabsCatalogue.fetch` already
  refused an empty source; this is the same rule one layer up, where the
  emptiness arrives from refusals instead.
- Tests for the catalogue client itself, and a `failure-modes.md` row for what
  they missed. The module shipped at **0% coverage** beside a 100%-covered
  sibling, passing only because the threshold is a global 85%. Its first
  version of *"no request ever goes over plain HTTP"* asserted that on the
  **success** path, and a mutation reintroducing an `http://` fallback in the
  `except` branch left every test green — a fallback that would have worked, and
  sent users' traffic unencrypted. The assertion is now on the failure path.

- **M2's read surface** — `GET /institutions?q=`, `GET /catalog/{name}` for the
  five lookup lists, `GET /users/{id}/education|goals|awards|mentor-profile`, and
  `GET /me` extended to embed all four so a profile page renders in one call. The
  sub-resources are addressed by user id rather than `/me/...` because a platform
  admin needs one user's education; `/me` calls the same store functions and the
  same response models, with a test asserting the two payloads are identical.
- **Institution search is tiered, and that is measured rather than chosen.**
  A single query combining the tiers with `OR` defeats the planner — `La` costs
  36.5 ms by `ILIKE`, 9.4 ms by trigram, and **123.6 ms** combined. Run
  separately, cheapest first, stopping as soon as the page is full: prefix,
  then substring, then a fuzzy pass at a 0.5 similarity floor. A typical
  keystroke costs about 6 ms and never reaches the fuzzy tier. Typo tolerance
  works when the term resembles the whole name (`Univerity of Lagos`); it does
  **not** rescue a short misspelled word against a long one (`Oxfrod`), which is
  stated in the route description and pinned by a test so the claim cannot
  quietly become false.
- **`ix_institutions_name_prefix`** on `lower(name) text_pattern_ops`. The GIN
  trigram index does not serve a prefix match, and prefix is autocomplete's
  common path — one query per keystroke — at 59.3 ms without the index and
  4.3 ms with it. `alembic check` **cannot** compare an expression index with an
  operator class (it says so and skips), so two tests cover what the gate
  cannot: the index is declared on the model, and present in the database.
- **`languages` holds 7,078 rows**, so the lookup lists take `?q=` and page by
  keyset rather than returning everything. The obvious trim — restrict to the
  174 ISO 639-1 codes — is ruled out by the schema's own reasoning: 639-3 was
  chosen precisely because the two-letter set omits Nigerian Pidgin. Closed
  vocabularies keep their `sort_order` and never page, so degree levels still
  read "Undergraduate, Diploma, Masters" rather than alphabetically.
- **Every unhandled exception is now Problem Details too.** Only deliberate
  `AppError`s were registered, so anything else — a database error, a bug, a
  third-party failure — left as Starlette's `text/plain` "Internal Server
  Error". That breaks ADR 0016's promise precisely in a client's error path,
  where a JSON parse failure turns one fault into two. Found by pointing the
  application at an unmigrated database. The detail is withheld (a database
  error names tables and columns) and the traceback is logged, because a 500
  that tells the caller nothing *and* the operator nothing is worse than the
  plain text it replaces.
- **`LIKE` wildcards in a search term are escaped.** The term is bound as a
  parameter, which stops injection and does nothing about the *pattern* being
  user-controlled: measured, `q=%` matched every institution, `q=____________`
  matched every name of twelve characters or more, and `q=100%` matched nothing
  where it should find "100% Academy". On a public unauthenticated endpoint that
  is also a one-character way to make every tier match everything.
- **ADR 0016's cursor rule is amended, scoped rather than reversed** — the id is
  the cursor for a list displayed in insertion order; a list displayed in some
  other order keys on its sort column plus the id. An id cursor over 7,078
  alphabetically-rendered languages pages by creation time and silently skips
  rows. The envelope, the cursor's opacity and "every list endpoint" are
  untouched.
- A `catalog` tag description, and the `users` one corrected: that group now
  serves `/users/{id}/...` for admins, not only the caller's own record.
- **`list_awards` returned soft-deleted awards**, so a user who removed one still
  saw it and so did an admin reading them. `ix_user_awards_user` is declared
  `WHERE deleted_at IS NULL`, so the query could not use the index built for it —
  a sequential scan where an index scan was intended. Found in review.
  `test_profile_store_soft_deletes` now takes the list of tables needing the
  predicate from `Base.metadata` rather than from anything a person maintains,
  so a new soft-deletable table fails until it is covered or exempted out loud.
  This is the second time this rule has been missed here — the first cost five
  statements on `users`, and the module-walk that fixed it does not transfer,
  because `profile_store` builds its statements inside functions.
- **Settled decisions 63, 64 and 65** — `status` filters search but never an
  entity read; catalogue reads are unauthenticated and tagged `catalog`; a merge
  repoints `education_entries.institution_id` rather than being resolved at read
  time.
- Two `failure-modes.md` rows, both found by mutation: four authorization tests
  that all stopped at the dependency, leaving the store's own `user_id` filter
  unexercised — dropping it would have returned every user's degrees to a caller
  entitled to their own; and a mutation harness that scored "no tests ran" as a
  kill, silently certifying untested code.

- **M2's write surface** — `POST/PATCH/DELETE` for education and awards,
  `PUT/DELETE` for the goal, `POST/PATCH` for a mentor profile, and `PATCH` for
  the user profile. **Owner only**: a live admin reads these records and never
  writes them, which is one clause of difference in the dependency and a test on
  every endpoint.
- **An unlisted school is created inside the education write's transaction**,
  `source='manual'`, `status='pending_review'`, `created_by` from the
  authenticated caller — so nothing anonymous reaches `institutions`, and a
  failure leaves neither the institution nor the entry. A name the catalogue
  carries twice is **queued, never linked**: `City University` is a real
  university in three countries and the study country derives from the choice.
  A new `CHECK (source <> 'manual' OR created_by IS NOT NULL)` enforces the
  invariant the review queue depends on.
- **Applying to be a mentor flips `primary_role`** so the applicant lands on the
  right dashboard. It grants nothing — `primary_role` picks a dashboard and is
  never an authorization claim; what a pending mentor may do follows from
  `approval_status`. **Nothing approves an application yet**, so these join
  institutions awaiting review in a queue with no owner.
- **`degree_levels` is now ISCED-aligned** — Certificate/Diploma, Bachelor's,
  Master's, Doctorate. The original six mixed education *levels* with a specific
  qualification (`MBA`, which is a master's degree) and a career stage
  (`Postdoctoral`, which is a research post — nothing is awarded), so a filter
  could not answer "mentors with a doctorate". Rows are repointed before the
  merged levels are deleted and `degree_category` keeps the raw legacy string,
  so nothing is lost. Done now because the only data in flight is test data.
- **The goal endpoints are singular.** `mentee_goals.user_id` is unique and the
  model says "1:1 with the user", so `GET/PUT/DELETE /users/{id}/goal` replaces
  a `Page` that could only ever hold zero or one. Found by a write test hitting
  the constraint on a second insert.
- **Lookup search is ranked, not alphabetical.** Twenty of the 7,078 ISO 639-3
  names contain "English", so searching for it returned Antigua and Barbuda
  Creole English first and the language the user meant fifth. Exact, then
  prefix, then anywhere — the tiering institution search already uses.
- **Every 422 is Problem Details.** FastAPI answers `RequestValidationError`
  itself with `{"detail": [...]}`, so the promise broke on the most ordinary
  failure there is: a form with a bad field. Nothing had exposed it, because the
  only 422 before writes came from our own error type.
- `PATCH` takes its own partial models. Reusing the create model made
  `school_name_raw` required on every edit, so a one-field change silently
  changed nothing.
- Inserts write **only the keys the client sent**. `payload.get(column)` over a
  column list wrote explicit `NULL`s, which override server defaults — applying
  to be a mentor without sending `requires_booking_confirmation` raised a
  not-null violation.
- **Settled decisions 66-69** and three `failure-modes.md` rows, including the
  one where a migration suppressed ruff's `S608` to build SQL by string
  formatting and was then broken by the apostrophe in "Bachelor's Degree".

- **The review surface** — `/api/v1/admin`, tagged `admin`: the pending
  institution queue ranked by how many education entries reference each row,
  approve, merge, the pending mentor queue, and a decision endpoint. **The only
  endpoints that show one user another's records by design**, so the control is
  the caller's grant rather than a row scope, and every one is tested against
  four cases: no token, no grant, a revoked grant, a live one.
- **Grants are role-specific.** `AdminRole` already distinguished
  `super_admin`, `mentor_approval` and `limited_access`; treating them as
  interchangeable would have made the enum decorative. `mentor_approval` decides
  applications and cannot curate the catalogue; `limited_access` reads and
  changes nothing.
- **Merging repoints, in one transaction** (settled decision 65). The losing
  row's entries move to the winner and it is marked `merged` together, so the
  chain collapses at merge time and no later read follows it. Merging into a
  row that is itself merged, or into itself, is a 409.
- **`declined_at` and `declined_by`.** `approved_by` had no counterpart, so a
  decline recorded *that* it happened and never *who* — and unlike a count that
  cannot be reconstructed afterwards. Reusing the approve pair would have put a
  decliner in a column named for approval, the shape `usage_count` and the old
  `updated_at` both failed in.
- **`languages.is_common`** marks the 100 languages in CLDR's `modern` coverage
  tier, with `GET /catalog/languages?common=true` serving the picker and search
  still reaching all 7,078. Replacing the table with the common set was measured
  and rejected: it would delete Efik, Ibibio, Tiv, Kanuri, Idoma, Urhobo, Nupe,
  Gbagyi, Esan, Ebira and Jukun — every one a Nigerian language, on a platform
  for African students. `pcm` is out of the default by our decision rather than
  the standard's, and stays searchable.
- `scripts/derive_common_languages.py` regenerates that list from CLDR, and a
  test asserts the seed still matches it. The migration holds a literal because
  a migration that fetches cannot be replayed.
- **`PUT /users/{id}/languages`**, carried over from the previous release's
  checklist. Replaces the whole list; at most one primary and no duplicates,
  both refused at the boundary with a 422 rather than surfacing a unique-index
  violation as a 500.
- **Settled decisions 70-72** and four `failure-modes.md` rows — including a test
  asserting `endswith("listed")`, which `"unlisted"` satisfies, and a seed
  literal typed by hand instead of pasted from its generator.

- **`mentor_status_events`, and the log becomes the write path.** Every approval
  and listing transition is recorded with who made it, when and why;
  `trg_apply_mentor_status` projects each event onto `mentor_profiles`, so
  application code inserts an event and never updates a status column. The two
  columns stay because `ix_mentor_profiles_searchable` indexes exactly them.
- **Seven columns go** — `approved_at`, `approved_by`, `declined_at`,
  `declined_by`, `decline_reason`, `unlisted_at`, `unlisted_reason`. Each held
  only the most recent decision, so a mentor declined, re-applied, approved and
  then unlisted had lost three of four. `ix_mentor_profiles_unlisted` goes with
  them and is not replaced: the query it served now runs against the log.
- **An admin can unlist an approved mentor without declining them** —
  `POST /admin/mentors/{id}/listing` — which is the third transition that made
  two columns per state stop scaling. `GET /admin/mentors/{id}/history` reads it
  back, newest first.
- **A mentor can pause and resume their own listing**, and **only their own**:
  resume is refused unless the newest unlisting carries `mentor_paused` and the
  mentor is approved. Otherwise a suspension would be a button the suspended
  person can press.
- **`scripts/check.py --fast`** — ruff, mypy, layers and unit tests, **~20
  seconds against 8–15 minutes** for the full gate. The suite is ~95% of the
  gate's runtime, so a one-line lint error cost a full cycle to find; three
  commits in the previous releases were rejected for something ruff answers in
  two seconds. It skips the database tests and coverage deliberately, and is not
  a substitute for the gate before committing.
- **Settled decisions 73-75**, four `failure-modes.md` rows, and one more entry
  on the `alembic check` blind-spot list — it cannot see triggers, and one now
  carries a rule.

### Changed

- **`calendar_connections` moves to M4**, against the M1 migration's statement
  that it ships with M3. Nothing in M3 reads or writes it, and ADR 0012 — which
  decides the OAuth arrangement its columns encode — is still Proposed with two
  behaviours it names as untested. Settled decision #80.
- **Every GitHub Action bumped off Node 20**, which is past end-of-life on the
  runners — 21 pins across 5 workflow files. `actions/checkout` v4 → v5,
  `astral-sh/setup-uv` v5 → **v7** (v6 is still Node 20), `actions/upload-artifact`
  v4 → v7, `softprops/action-gh-release` v2 → v3, `gitleaks/gitleaks-action`
  v2 → v3. Each target's `runs.using` was read from its own manifest, and every
  input this repository passes was confirmed to still exist in the new major.

- **ADRs 0004 and 0009 carry correction notes: Google OAuth verification is not on
  the critical path, and never was.** Both records treated sensitive-scope review
  as unavoidable — 0004 stating that "Google Calendar scopes are in the *sensitive*
  tier" and gating calendar connect on weeks of queue time, 0009 calling
  verification "the long pole" that "gates the majority login path". Checked
  against the scope list in the Google Cloud console rather than recalled, the
  scopes both decisions actually need are **non-sensitive**: `openid`,
  `userinfo.email` and `userinfo.profile` for sign-in, and `calendar.freebusy`
  plus `calendar.app.created` for calendar — the latter granting full create,
  change and delete on secondary calendars the application makes. No app review,
  no user cap, no unverified-app warning. Neither decision changes; both are
  better served, because the scope pair is strictly narrower than either record
  contemplated. Recorded as **v1 of the Google integration** — sensitive scopes
  remain available later, with a record of their own, if a capability needs them.
  Original text left intact in both, per the convention ADRs 0002 and 0003 set.
- **ADR 0008 is accepted**, and no live assertion of ROR survives. Settled
  decision 17 becomes the hipolabs registry — populated on demand, surrogate
  `uuid` primary key, `domain` as the natural key, no `ror_id` — with the
  reasoning that the dependency lands on writes only and that Bubble already runs
  on hipolabs autocomplete. The `ror_id` vocabulary term is replaced by
  `institutions.domain`; that one mattered more than the decision row, because it
  read as an instruction to create a column the target schema does not have.
  `README.md`'s stack table is corrected to match.
- **ADR 0007's status now names its resolved deferrals** — institutions by ADR
  0008, first-login authentication by ADR 0009, with message-thread scope still
  reserved for ADR 0010. ADR 0002's status already carried its transport deferral
  this way and 0007 did not, which made two records with the same shape read
  differently. Its deferral table is deliberately left as written, including the
  stale claim that institutions block M0.
- **ADR 0009 is accepted**, and the documents that assumed a magic link now agree
  with it. Settled decision 7 describes the decided mechanism — OAuth for Google
  or LinkedIn, and on email a 6-digit code by default with a sign-in link as a
  choice — while its substance and its reopen condition are unchanged. Settled
  decision 12 no longer justifies Supabase on *magic-link* login specifically;
  passwordless email login still arrives without being built, which is what that
  argument needed. ADR 0002's status names **point 9** as superseded on the
  mechanism, restructured as a list so three qualifications stay readable, and its
  body is untouched. ADR 0004 is deliberately left alone: it cites decision 7 for
  the fact that users are already re-authenticating when the calendar reconnect
  arrives, and that holds whichever mechanism they are in.
- Target Python 3.14 across `.python-version`, `requires-python`, ruff and mypy.
- CI tests a single interpreter instead of a 3.12/3.13 matrix — this is a
  deployed application, not a library consumed on many Python versions.
- CI and the release workflow select steps from `scripts/check.py` with `--only`
  instead of restating the commands, so the gate is defined once. The bandit
  step gains `-c pyproject.toml`, which the restated CI copy had dropped.
- `[tool.check-layers.forbidden-external]` now covers `api` and `core` as well as
  `domain`, and names the adopted vendor SDKs, so "no vendor SDK outside `infra/`"
  is enforced rather than only documented. It is a denylist: a newly adopted
  vendor is unguarded until its package name is added alongside the dependency.

### Fixed

- **Search could not find a mentor by the subject printed on their card.** The
  document indexed `study_program`, which holds degree *names* like `BSc
  (Bachelor of Science)` and is populated on 8 of 21 dev-export rows. The card
  displays `study_course` — `Mathematics`, `Physics` — populated on 21 of 21.
  Searching the word every card shows returned nobody, and no existing test
  noticed because each of them searched by name, school or country.

  `study_course` is now in the document **beside** `study_program`, not instead
  of it: "Bachelor of Engineering" is a real query and 8 rows carry one. Measured
  A/B in a single process, it costs ~16% at 5,000 mentors and nothing measurable
  at 500.

  The two columns had a second copy of the same confusion in the tests: two
  `add_education` helpers with the same name in one suite, one writing
  `study_program` and the other `study_course`. Consolidated into the shared
  factory with both named apart, since a helper that wrote whichever column its
  author had in mind is how the fields got conflated to begin with.

- **The institution mirror stamped `updated_at` from two different clocks.** The
  insert omitted the column and took the `now()` default; the update set
  `:synced_at`. Which one a row got depended on which branch of the upsert it
  hit. In production the two are the same instant so nothing showed, and against
  a fixed historical `synced_at` a row was stamped with the wall clock instead.

  Found because `test_a_changed_name_does_move_updated_at` compared the two and
  so asserted `SYNCED + 7 days > now()` — true for exactly seven days after it
  was written, false from 04:00 UTC on 2026-08-15 and every day after. The test
  was the messenger; the two clocks were the defect. Both paths now stamp
  `:synced_at`, and a test asserts each path directly rather than through an
  ordering, because the ordering is what hid it.

- **Client text the database cannot store was an unauthenticated 500.** Two
  causes, failing in two different layers:

  `U+0000` encodes to UTF-8 perfectly well and PostgreSQL then refuses the
  value — `CharacterNotInRepertoireError`. A single anonymous
  `GET /institutions?q=%00` produced a stack trace, and so did
  `GET /catalog/{catalogue}` and every free-text write body.

  An **unpaired surrogate** never reaches PostgreSQL at all: UTF-8 has no
  encoding for one, so asyncpg raises `UnicodeEncodeError` while building the
  message. JSON carries it as a plain-ASCII escape, so a request body is a live
  route. Reachable through exactly one field —
  `AvailabilityExceptionWrite.reason`, the only free-text field in the API with
  no `max_length`. A length constraint makes pydantic-core parse the value as
  unicode and refuse it with a 422 first, which is why every other field was
  already safe, by a mechanism nobody here chose. A test now pins that
  behaviour, because if it changes every field becomes a route.

  Removed rather than refused, which is what `Normalised` already does to the
  text it is given: it trims, and turns `""` into `None`. A base class that
  quietly repairs whitespace while hard-failing a control byte would be two rules
  wearing one name.

  One rule, two adapters — `storable()` in `schemas/common.py`, reached by
  `Normalised` for bodies and by the `StorableText` annotation for query
  parameters, so a fourth text parameter inherits it rather than remembering it.
  It is defined as *what cannot be stored* — encode with the database's encoding
  and drop what it cannot represent, then drop the one thing it can represent and
  the server still rejects — rather than as a list of characters, because the
  first version of it was a list and the list was wrong. Every other control
  character survives, including the C0 range, non-characters and astral-plane
  emoji.

  `AvailabilityExceptionWrite.reason` was also the only free-text field outside
  the shared base and had never been trimmed; its mixin now inherits `Normalised`
  like everything else. One field being the odd one out twice, for two unrelated
  reasons, is the argument for the rule living in a base class.

  No OpenAPI change — the generated schema is byte-identical before and after.
- **A search handed out a `next_cursor` its own decoder refused.** At the depth
  cap the endpoint minted a token for the next offset without checking it,
  and the following request answered 422 — so a client that followed the
  envelope exactly ended a deep search on an error instead of `null`. The cap was
  compared in the decoder and nowhere else; both directions now ask one
  predicate. The lower bound was untested and is now covered: a fabricated
  negative offset is refused rather than reaching Postgres, which rejects a
  negative `OFFSET` outright.

- **`HTTP_413_REQUEST_ENTITY_TOO_LARGE`** replaced with
  `HTTP_413_CONTENT_TOO_LARGE`; the old spelling is deprecated in Starlette and
  emitted a warning on every refusal.
- The shared `settings` test fixture no longer reads the developer's `.env`;
  any field it did not pin explicitly was taking that file's value.
- Settled decisions 12–19, and two rows in `references/failure-modes.md`, were
  separated from their table headers by a blank line and so rendered as plain
  text rather than as table rows. Both tables are contiguous again.
