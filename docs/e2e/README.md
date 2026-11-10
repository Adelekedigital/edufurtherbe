# Launch end-to-end test plan

One source of truth for end-to-end (E2E) testing before launch. AI agents and the
human team run the same scenarios, by the same ids:

| File | What it is | Who reads it |
|---|---|---|
| [`scenarios.yaml`](scenarios.yaml) | Every scenario: the seed accounts, API steps, UI steps, side-effect checks | The backend agent runs `kind: api` steps and `assertions`; the frontend agent runs `kind: ui` steps with Playwright |
| [`scenario.schema.json`](scenario.schema.json) | The shape of one scenario | Both agents, and `tests/unit/test_e2e_scenarios.py` |
| *(later)* a shared test doc | Generated from the YAML: `title`, `why` and each step's `human` text | Human testers and the founder |

`tests/unit/test_e2e_scenarios.py` runs in CI: the YAML must match the schema,
every API step must be a route the app serves, every `viewer` a seed account and
every `after` an earlier step, and nothing may look like a secret.

## Scope by journey

**P0 (blocks launch), written in full for the backend:**

| Journey | Ids |
|---|---|
| Sign-in and first provisioning | `AUTH-001` |
| Mentee onboarding | `ONB-001` |
| Mentor application, admin approval, listed and bookable | `MENTOR-001` |
| Setup: onboard the booking mentees | `SETUP-001` |
| Explore, profile, book, accept, join, settle; no-show | `BOOK-001`, `BOOK-002` |
| Mentee cancel inside and outside the refund window | `CANCEL-001`, `CANCEL-002` |
| Mentor decline | `DECLINE-001` |
| Reschedule (move) | `MOVE-001` |
| Review after a completed session | `REVIEW-001` |
| Monthly credits and level-up | `CREDIT-001` |
| Admin credit grant | `CREDIT-002` |
| Messages between a booked pair | `MSG-001` |
| Account deletion and purge | `ACCT-001` |
| Preview as user, read only | `PREVIEW-001` |
| Admin analytics behind its flag | `ANALYTICS-001` |
| Analytics consent | `CONSENT-001` |
| Acquisition, write once | `ACQ-001` |
| Production smoke (GET only) | `SMOKE-001`, `ANALYTICS-001` |

Booking needs 24 hours' notice, so the join, settle and late-cancel steps run
in a **second sitting** after the booked time; `after` marks them. Scheduled
jobs (settlement, monthly credits, purge) are `kind: job` steps run with their
`scripts/` entry point, because the HTTP job route needs a QStash signature.

**P1 and P2** are stubs (`steps: []`) until their journey is written: guest
booking, invites, deep links, OAuth connects, PWA, push prompt, time zones, the
mentor landing redirect, private files, error states, the consent banner,
keyboard-only booking, 390px layout, and the two human-only sign-ins.

## Environments

| Environment | What runs | How |
|---|---|---|
| **Staging** | Everything | API at `E2E_API_URL`, frontend at `E2E_FRONTEND_URL` |
| **Production** | Only scenarios tagged `smoke_prod: true` | GET-only, no seed, no session script; a human signs in |

**Staging is the backend service in Railway's `development` environment, and
the Supabase project that service uses** (confirmed by the founder). The
frontend is the Vercel app built against that same Supabase project. The
operator sets, on their own machine and never in this repository:

| Variable | Set to |
|---|---|
| `E2E_API_URL` | That backend service's public URL. Required. |
| `E2E_FRONTEND_URL` | That frontend's URL. Required for UI steps. |
| `E2E_SUPABASE_PROJECT` | That service's Supabase project ref. Required: `e2e_session.py` refuses unless `SUPABASE_URL` names this project. |
| `E2E_ALLOW` | `1`, only while running. |

Staging comes first. Production gets the read-only smoke set after the release
is promoted.

## Test data and accounts

- Every E2E account is named by a **ref** in `scenarios.yaml` → `seed.accounts`
  (`mentee_new`, `mentee_with_credits`, `mentor_listed`, …). The YAML never
  holds an address.
- Addresses are on the reserved demo domain `demo.edufurther.test`
  (`DEMO_EMAIL_LIKE` in `src/app/infra/db/demo_seed.py`):
  `e2e-<run>-<ref>@…`, with `_` written `-`. No real inbox exists, so nothing
  can reach a person. Demo-domain accounts are internal (decision 329), so
  they write no analytics events.
- **Seed, once per run:** `scripts/seed_demo_mentors.py --e2e <run>` creates
  the accounts in `E2E_ACCOUNTS` under a new run tag (1 to 16 lowercase letters
  or digits, e.g. `r20261110a`); a tag already used is refused. Each run starts
  clean and nothing is ever deleted: an account with history is held by dozens
  of `RESTRICT` keys, and old runs are ordinary dev data. Demo removal
  (`--remove`, `--avatars`) never touches E2E accounts. It refuses production.
  `mentee_new` is not seeded: its first sign-in creates it (`AUTH-001`).
- **Sessions:** `scripts/e2e_session.py` signs an agent in as a seed account (see
  below). It runs on the tester's machine, never on a deployed server.

### Signing in as a seed account

```bash
export E2E_ALLOW=1 E2E_SUPABASE_PROJECT=<the ref>   # plus the staging service env, e.g. `railway run`
uv run python scripts/e2e_session.py --env staging --run r1 --account mentee_with_credits --token
uv run python scripts/e2e_session.py --env staging --run r1 --account mentee_with_credits --link \
    --redirect "$E2E_FRONTEND_URL/auth/callback"
```

- `--token` prints an access token for the API runner; `--link` prints a
  one-time magic link for the browser runner. Only that goes to stdout.
- It uses Supabase's admin `generate_link`, which sends no email, then
  `/auth/v1/verify` for the token. The address must be an E2E account
  (`e2e-<run>-<ref>@` the demo domain, `ref` in `E2E_ACCOUNTS`); anything else
  is refused before a request is made. It creates the Supabase sign-in
  silently if missing and points the seeded row at it.
- It refuses unless `E2E_ALLOW=1`, `--env staging` is given,
  `EDUFURTHER_ENVIRONMENT` is explicitly `staging`, and `SUPABASE_URL` names
  the `E2E_SUPABASE_PROJECT` project. Production cannot match.
- The token and link are never logged; treat them as passwords and do not paste
  them into issues or run reports.

## Roles

| Who | Does |
|---|---|
| Backend agent | Seeds staging, runs `kind: api` steps and `assertions`, files defects |
| Frontend agent | Runs `kind: ui` steps with Playwright at the given viewports, with screenshots and an accessibility scan where asked |
| Human testers | Run each P0 by hand from the generated doc, and every `human_only` scenario |
| Founder | Signs off: go or no-go against the exit criteria |

## Entry criteria

1. Staging is migrated to the Alembic head of the commit being tested.
2. The flags each scenario lists are set on staging (backend and `NEXT_PUBLIC_*`).
3. `seed_demo_mentors.py --e2e <run>` has run on staging with a new tag, and
   `SETUP-001` has passed for it.
4. CI is green on the exact SHA deployed to staging.

## Exit criteria

1. Every P0 passes, run by an agent **and** by a human.
2. No open P0 or P1 defect.
3. Every P2 is triaged: fixed, or an issue with a decision.
4. The production smoke set passes after promotion.

## Definition of Done for one scenario

- Every step ran against staging and met its `expect`.
- Every `assertion` held.
- UI steps: screenshots attached and no serious accessibility violation where
  `a11y: true`.
- `cleanup` ran (most scenarios need none: the next run uses a new tag).
- The result is in the run report, with the deployed SHA.

## Severity

| Severity | Meaning |
|---|---|
| **P0** | Blocks launch: money or credits wrong, a data leak, or someone cannot sign in, book, join or settle |
| **P1** | A major journey is broken but has a workaround |
| **P2** | Minor: cosmetic, copy, an edge case |

## Defects

One GitHub issue per defect, labelled `e2e`, `e2e:<scenario id>` and its
severity (`P0`, `P1`, `P2`). The body names the step id, expected and actual
result, the deployed SHA and a link to the run report. Never paste a token, a
link from `e2e_session.py`, or a real person's data.

## Run reports

One file per run: `docs/e2e/runs/<YYYY-MM-DD>-<env>.md`, or
`docs/e2e/runs/<YYYY-MM-DD>-<env>.json` from an agent. Each holds:

- environment, deployed SHA, Alembic head, who ran it (agent or human), and when;
- one row per scenario: id, result (`pass`, `fail`, `blocked`, `skipped`),
  failing step id, defect link;
- the verdict: how many P0s pass, and any open P0 or P1.

JSON shape: `{"env", "sha", "runner", "started_at", "results": [{"id", "result",
"step", "defect"}]}`.

## Adding a scenario

1. Pick the next free id for its journey prefix (`BOOK-003`).
2. Write `title`, `why` (cite the decision number when one exists) and each
   step's `human` text in plain words: they become the human doc.
3. API steps use the path exactly as in the OpenAPI
   (`uv run python scripts/export_openapi.py --out openapi.json`).
4. UI targets are accessible role and name, or a label. Never a CSS selector.
5. A new account goes in `E2E_ACCOUNTS` and in `seed.accounts` together; the
   test pins them equal.
6. Run `uv run pytest tests/unit/test_e2e_scenarios.py`.
7. A rule you cannot find in the code is a `TODO(question)`, never a guess.

## Security rules

This repository is public.

- No secret, token, code, magic link, real email or name, staging or production
  hostname, or Supabase project ref in any file here, an issue or a report.
- Secrets are referenced by environment variable **name** only.
- Test accounts live only on the demo domain; the session script refuses any
  other address and any environment but staging.
- Database checks in `assertions` are read-only `SELECT`s.
