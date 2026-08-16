# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`.github/workflows/release.yml` parses the section matching the tag being
released. A tag with no matching section here fails the release job.

## [Unreleased]

### Fixed

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
