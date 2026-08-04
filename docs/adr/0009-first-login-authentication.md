# 9. First-login authentication: Supabase Auth, with an email code by default and the link as a choice

Date: 2026-08-04

## Status

Accepted

## Context

ADR 0007 deferred this and named it as blocking **M1, the critical phase** —
~2,100 rows on which everything downstream depends. Nothing in the migration can
proceed past the foundation until it is decided.

**Every user re-authenticates, and that is a constraint rather than a choice.**
Bubble password hashes are not exportable (settled decision #7, ADR 0002), so all
1,200 accounts match on email and complete a fresh first login. Whatever is chosen
here is the first thing every migrated user touches, and the point at which the
migration is most visible to them.

**Four records already assume a magic link**, which is more of this corpus than
it first appears. Settled decision #7 says "every user does a magic-link or reset
on first login". **ADR 0002 point 9** states the same in the export strategy. ADR
0004 leans on it — "settled decision #7 already requires every user to
re-establish identity by magic link at first login after the cutover freeze, so
the calendar reconnect is one more step in a flow they are already in". And ADR
0005 chose Supabase partly because "passwordless email login arrives without being
built", leaving an explicit open question: *whether Supabase Auth's magic-link
flow satisfies decision #7 exactly, in particular for the 1,200 migrated users
matching on email — **not yet tested**.* Only ADR 0002 point 9 is a decision;
the other three cite it.

**The migration package disagrees, with specific reasons.** D26 chooses a 6-digit
OTP over a magic link on two concrete failure modes: Outlook Safe Links prefetches
and consumes the token before the human clicks, and WhatsApp's in-app browser
opens the link in a different session from the user's real browser. Both are real,
both are common in this user base, and both produce a login that fails while
looking like it should have worked. D26 pairs that choice with eight controls it
calls non-optional, because six digits is a million combinations.

**New evidence, not available when either record was written.** The legacy `User`
table carries `Registration format`, an option set of (Email, Google, LinkedIn)
— `docs/bubble-data-model.md`. The product owner reports **Google was the dominant
signup path**. That inverts the shape of the problem: for most of the 1,200, first
login is an OAuth click that returns the same verified email we matched on, and
the email path is the minority case rather than the main one.

**What Supabase actually does, verified against its documentation rather than
recalled:**

| Capability | Finding |
|---|---|
| 6-digit email OTP | Supported, by putting `{{ .Token }}` in the Magic Link template |
| Magic link **and** OTP in one email | **Not workable.** The template switches between them; where both variables are used, community reports the `Token` does not validate |
| Request rate limit | One per 60 seconds per user |
| OTP expiry | **One hour by default**, configurable |
| Code length | Not documented as configurable |
| Per-code attempt locking | **Not documented** |
| Send Email Hook payload | Carries **both** `token` (the 6-digit code) and `token_hash` (from which a link is built), and the hook composes the email itself |
| Send Email Hook plan availability | **Free and Pro** — not gated behind a paid tier, so it does not force the plan decision ADR 0005 left open |

So one email carrying both is not workable, but **letting the user choose which
they receive is** — through a Send Email Hook, where we compose the message and
therefore decide which representation to use. The hook is an endpoint we own; it
can read the user's preference from our own database, because it is our code.

That also resolves something the stack implies anyway. The README already lists
**Emailit** as the transactional email provider, behind a port. Without a hook,
authentication email is the one category that would leave Supabase's default SMTP
instead — a second sender, a second deliverability reputation, and the messages
most likely to be marked spam are exactly the ones that must arrive.

## Decision

**Supabase Auth is the identity provider for first login and every login after
it. On the email path a 6-digit code is the default and a sign-in link is offered
as a choice; the magic link is what stops being the default, not what stops being
available.**

1. **OAuth for Google and LinkedIn**, which covers the majority path and makes
   first login a single click for most migrated users.
2. **Email OTP is the default**, and it answers D26's two failure modes directly:
   a prefetched link cannot burn a code, and a code can be typed into the browser
   the user is already standing in. Nobody has to know why it is a code.
3. **The magic link is offered as a choice, not as the default** — "email me a
   sign-in link" alongside "email me a code" — delivered through a **Send Email
   Hook** that composes both forms from the same request. The default protects
   the users who would silently fail; the choice serves the ones who prefer a
   click and know their mail client is fine.
4. **Authentication email goes through our own sender** (Emailit, behind the
   existing port) because the hook makes that automatic rather than extra. One
   sender, one reputation, one place to look when a message does not arrive.
5. **OTP expiry is set to 10 minutes**, not the one-hour default. A six-digit
   code that lives for an hour is a million combinations with an hour to work
   through them. This is one setting and it is not optional.
6. **An auth identity is auto-linked to a migrated user only when the provider
   asserts the email is verified.** Google and LinkedIn both return
   `email_verified`. Matching on the email string alone would let anyone who can
   register that address at any provider claim a migrated account, and the
   accounts most worth claiming are the mentors.
7. **`auth_codes`, `auth_code_purpose` and `users.password_hash` are dropped from
   the target schema.** Supabase owns code issuance, hashing and verification, and
   it owns passwords too — a table or column we never write to is schema asserting
   an implementation we do not have. The package keeps `password_hash` nullable so
   admins can hold a password beside OTP (D26); that account lives in Supabase
   with the same reasoning and none of the storage.
8. **`auth_identities` stays**, and the asymmetry with point 7 is deliberate.
   Supabase has its own `auth.identities`, so this is the one place we duplicate
   it. It is kept because D28's reasons are ours rather than the vendor's:
   multiple providers per user, account linking on email collision, and — the
   one that decides it — `Registration format` is a legacy column we must land
   somewhere on import, and it maps to `provider`
   (`docs/edufurther-migration/docs/02_FIELD_MAPPING.md`). A migrated fact needs a
   row in our schema; an operational mechanism does not.
9. **`users.id` is the Supabase auth user id**, and **`DEFAULT uuid_generate_v7()`
   comes off that column**. One identifier, no mapping table, no second id space
   to translate at the boundary — but the default would quietly mint a valid-looking
   id for any insert that forgot to pass the auth one, producing a row that can
   never be logged into and looks entirely normal. `users` is therefore the one
   table whose id is not time-ordered: Supabase issues v4. Nothing downstream
   depends on that ordering, and a UUIDv7 was never safe as a sort key here anyway.

**The user's delivery preference is stored on our own `users` row**, not in
Supabase metadata, because the hook reads it and the hook is our code. It is a
nullable column: null means the default, which means a code.

### Rejected alternatives

**Magic link only**, as decisions #7 and ADR 0002 assume. Rejected on D26's two
failure modes, which are not hypothetical: Outlook Safe Links prefetching is
default behaviour in the tenants many mentors' institutions run, and the WhatsApp
in-app browser is how a large share of this user base opens anything. A login
mechanism that fails silently for a predictable segment is worse than one that is
slightly less convenient for everybody.

**Both in one email, letting the user click or type whichever they prefer.**
Investigated first, and rejected because it does not work with the built-in
template: the template switches between the two, and where both variables are
present the code does not validate. Recorded so the next person does not spend the
same afternoon on it. Note this is *not* the same as the decision above — a user
choosing a delivery method in advance is served by the hook; two mechanisms live
in one message is what fails.

**A single mechanism with no choice at all.** Simpler, one path to test, one
support answer. Rejected because the two populations genuinely differ: a mentor at
an institution running Outlook Safe Links needs the code, and a mentee who lives
in Gmail finds a link faster. The cost of offering both is one column and one
branch in an email template we are writing regardless — and it is only cheap
*because* the hook already has to exist to route auth mail through our own sender.
Without the hook this would not be worth it.

**Build the OTP system the package specifies** — `auth_codes`, hashing,
attempt-locking, single-active-code, per-destination rate limits. This is the
only option that delivers all eight of D26's controls under our own control.
Rejected because it rebuilds the thing ADR 0005 chose Supabase to avoid, on the
critical path of a migration, and because the controls it would add protect a
minority login path. **It is the right answer if OTP abuse ever materialises**,
and the schema for it is already written in the package.

**A different auth vendor.** Rejected without much deliberation: ADR 0005 is four
days old, the reasoning in it has not changed, and swapping the auth vendor now
would reopen storage and database along with it.

## Consequences

**Settled decision #7 and ADR 0002 point 9 are superseded on the mechanism, not
the substance.** Both say "magic link"; the substance — identity does not migrate,
accounts match on email, every user re-authenticates passwordlessly — is unchanged.
On acceptance, tier 2 decisions #7 and #12 are reworded, and ADR 0002's status
gains `point 9 superseded on the mechanism by ADR 0009` beside the anchor-column
note it already carries. **ADR 0004 needs no status change**: its argument is that
users are already in a re-authentication flow when the calendar reconnect arrives,
and that holds whichever mechanism they are in — only its illustrative "by magic
link" goes stale.

**ADR 0005's open question is answered.** Supabase Auth does satisfy decision #7,
via OTP rather than the magic-link flow that record assumed.

**Three of D26's eight controls are unverified or weaker, and that is the weakest
part of this decision.** Mapped one by one rather than summarised, because the
three that are short reinforce each other:

| D26 control | Under Supabase |
|---|---|
| 1 hash · 6 constant-time · 7 uniform response · 8 CSPRNG | Presumed — internal to the vendor, and not ours to check |
| 4 · 10-minute expiry | Configuration. Default is **one hour**, six times D26 |
| 2 · lock the **code** after `max_attempts` | **Not documented** |
| 3 · one active code per (user, purpose) | **Not documented** |
| 5 · 3 per 15 min per destination | **Weaker.** One per 60s per user ≈ **15 per 15 min** |

Expiry is fixed by setting it. The other three are the exposure, and they multiply
rather than sit side by side: control 3 exists precisely because concurrent live
codes shift the odds against a 1,000,000-space, and control 5 is what bounds how
many can be requested. If codes are not invalidated on reissue *and* fifteen can be
requested per window *and* nothing locks a code after repeated wrong guesses, the
per-user 60-second **request** limit does nothing — it never governed **verification**
attempts. **All three must be tested against a real project before cutover**, and if
they are absent the answer is a shorter expiry, a longer code, or building the
package's `auth_codes` after all.

**The Send Email Hook is now on M1's critical path**, and it was not in any
earlier plan. It is an endpoint that must exist, be reachable by Supabase, and be
correct before a single migrated user can log in — a failure there is a total
login outage rather than a degraded one. Against that: it is the same endpoint
that routes auth mail through Emailit, which we would otherwise have to accept
losing. The work is real and it is not large; the risk concentration is the part
worth watching.

**Google OAuth verification is now the long pole and it is calendar time, not
engineering time.** Sensitive-scope verification is weeks of paperwork. It gates
the majority login path. It should start the day this record is accepted.

**Lock-in deepens exactly where ADR 0005 said it would.** That record named auth
records as "the sticky part, stickier than the data" — moving them later means a
second forced re-authentication for every user. This decision commits to that,
knowingly, in exchange for not building auth during a migration.

**Phone and WhatsApp verification are not covered here.** Collecting phone numbers
is coming — the handoff notes WhatsApp reaches nobody whose number we do not have
— and Supabase's phone auth is a different mechanism with different providers. If
WhatsApp OTP through our own Meta templates is wanted, the package's `auth_codes`
may return for that purpose alone. Deferred, not decided.

### Confirmation

All three of the checkable items below are **`Mechanical, once built`** rather
than mechanical: identity lands in M1, and none of these can run against a chain
that stops at reference data. Labelling them plain `Mechanical` would put this
record's own confirmations in the category the section exists to keep them out of.

- **Mechanical, once built:** no `auth_codes` table, no `auth_code_purpose` type
  and no `users.password_hash` column appear in the schema. Their absence becomes
  visible in the migration chain when M1 writes the identity tables — until then
  the chain creates nothing for them to be absent from.
- **Mechanical, once built:** `users.id` carries no `DEFAULT`. The every-model
  test that already asserts the timestamp rule is where this belongs, and a
  default reintroduced later would otherwise be invisible until an orphan row
  appeared. The assertion is written with the `users` model, not before it.
- **Mechanical, once built:** a test asserts that account linking rejects an
  unverified provider email, once the linking code exists in M1.
- **Not mechanical:** nothing verifies the OTP expiry is actually set to 10
  minutes in the Supabase project. It is dashboard configuration, invisible to
  this repository, and it will look identical whether or not anybody set it.
- **Not mechanical, and the largest gap:** attempt-locking, code invalidation on
  reissue, and the real per-destination request ceiling are all unmeasured. This
  record asserts a security posture on three counts it has not tested.
- **Not mechanical:** the first login flow cannot be tested end to end until M1
  builds it, so this decision is made on documentation rather than on a working
  flow — the same caveat ADR 0007 made about adopting a schema on reading.

### Open questions

- **The actual registration split.** `Registration format` is in the export and
  the count has not been run — the Bubble extract does not exist yet. It does not
  change this decision, which covers all three paths, but it decides how much
  polish the email path deserves and should be recorded when M1 starts.
- **What Supabase actually does about brute force** — whether it locks a code
  after repeated failed verifications, whether requesting a new code invalidates
  the previous one, and what the effective per-destination request ceiling is.
  Named above as the three short controls; answered together against a real
  project, before cutover rather than before M1.
- **Whether phone verification needs the package's `auth_codes` after all.**
  Deferred to whenever phone collection is built.
