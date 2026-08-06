"""Provisioning decisions, and the Admin API adapter that carries them out.

**The test this file exists for is ``test_no_endpoint_that_sends_email_is_ever_
called``.** Everything else here can be found by a failing run in staging. That
one cannot: reaching ``/auth/v1/invite`` instead of ``/auth/v1/admin/users``
succeeds, returns 200, and mails 1,200 people who did not ask to be mailed.
There is no undo and no failing assertion in production to warn you first.

It was proved by mutation — pointing ``create_user`` at ``INVITE_ENDPOINTS[0]``
and confirming that exactly this test went red while the rest stayed green.
"""

import json
from collections.abc import Callable
from types import ModuleType
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.provisioning import Action, Candidate, Outcome, decide
from app.infra.auth.admin import (
    INVITE_ENDPOINTS,
    LOOKUP_PAGE_SIZE,
    MAX_ATTEMPTS,
    MAX_LOOKUP_PAGES,
    AdminApiError,
    SupabaseAdminClient,
)

BASE = "https://project.supabase.co"
KEY = "service-role-key-never-logged"
EMAIL = "ada@example.com"

#: GoTrue's paging rule, supplied by the `gotrue_paging` fixture so this suite
#: and the integration suite share one representation of it.
type PagingRule = Callable[[list[dict[str, str]], httpx.Request], httpx.Response]


class Recorder:
    """A transport that answers from a script and remembers every request."""

    def __init__(
        self,
        *responses: httpx.Response,
        answer: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self._responses = list(responses)
        # `answer` replaces the script when a test has to react to the request —
        # paging, for instance, which a flat list of responses cannot express.
        self.answer = answer
        self.requests: list[httpx.Request] = []
        self.slept: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answer is not None:
            return self.answer(request)
        # The last scripted response repeats, so a test only has to script the
        # responses it cares about.
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self) -> SupabaseAdminClient:
        return SupabaseAdminClient(
            base_url=BASE,
            service_role_key=KEY,
            client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            sleep=self.slept.append,
        )

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def account(identifier: UUID, email: str = EMAIL) -> dict[str, str]:
    return {"id": str(identifier), "email": email}


def ok(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# --------------------------------------------------------------------------
# the guarantee
# --------------------------------------------------------------------------


def test_no_endpoint_that_sends_email_is_ever_called() -> None:
    """Creating an account must not notify the person it belongs to.

    Migrated users are given an account during a freeze, before they are told
    anything. An invitation email arriving from a system they have not been
    introduced to yet reads as a phishing attempt, and 1,200 of them cannot be
    recalled.
    """
    supabase = uuid4()
    recorder = Recorder(ok({"users": [account(supabase)], "id": str(supabase), "email": EMAIL}))
    client = recorder.client()

    # Every method, not only the one the guarantee was written for. A future
    # lookup helper reaching `generate_link` sends mail just as effectively.
    client.create_user(EMAIL)
    client.find_by_email(EMAIL)
    client.get(supabase)

    assert len(recorder.paths) >= 3, "the test proves nothing if no request was made"
    for path in recorder.paths:
        assert path not in INVITE_ENDPOINTS, f"{path} sends email"


def test_create_marks_the_address_confirmed() -> None:
    """These users verified their address in Bubble years ago. Without
    ``email_confirm`` Supabase holds the account unconfirmed and the first login
    fails for a reason nobody can act on."""
    recorder = Recorder(ok(account(uuid4())))

    recorder.client().create_user(EMAIL)

    assert recorder.requests[0].method == "POST"
    # Parsed rather than matched as bytes: httpx's separators are its own
    # business, and a test that breaks when they change tests the wrong thing.
    assert json.loads(recorder.requests[0].content) == {"email": EMAIL, "email_confirm": True}


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------


def test_an_address_that_already_has_an_account_returns_it() -> None:
    """The resume path. A previous run created this account and died before
    recording it; failing here would strand the user permanently."""
    supabase = uuid4()
    recorder = Recorder(
        ok({"error_code": "email_exists", "msg": "already registered"}, status=422),
        ok({"users": [account(supabase)]}),
    )

    assert recorder.client().create_user(EMAIL).id == supabase


def test_a_partial_address_match_is_not_a_match() -> None:
    """Supabase's ``filter`` is a substring search, so a lookup for
    ``ada@x.com`` can return ``ada@xy.com``. Linking that account would give one
    person's login to another."""
    recorder = Recorder(ok({"users": [account(uuid4(), "ada@xy.com")]}))

    assert recorder.client().find_by_email("ada@x.com") is None


def test_a_missing_account_is_an_answer_rather_than_an_error() -> None:
    recorder = Recorder(ok({"users": []}))

    assert recorder.client().find_by_email(EMAIL) is None


def test_a_lookup_failure_is_not_reported_as_a_missing_account() -> None:
    """A 500 read as "no such user" would create a duplicate account."""
    recorder = Recorder(ok({}, status=500))

    with pytest.raises(AdminApiError):
        recorder.client().find_by_email(EMAIL)


def test_get_reports_an_id_supabase_does_not_have() -> None:
    recorder = Recorder(httpx.Response(404))

    assert recorder.client().get(uuid4()) is None


def test_a_response_with_no_id_is_refused() -> None:
    """Returning a half-built ``AuthUser`` would write null into ``users.auth_id``
    and report success."""
    recorder = Recorder(ok({"email": EMAIL}))

    with pytest.raises(AdminApiError):
        recorder.client().create_user(EMAIL)


def test_no_error_message_carries_the_service_role_key() -> None:
    """The key is in every request header, and an error quoting the request would
    put it in a terminal and then in a CI log."""
    recorder = Recorder(ok({"msg": f"rejected key {KEY}"}, status=403))

    with pytest.raises(AdminApiError) as raised:
        recorder.client().create_user(EMAIL)

    assert KEY not in str(raised.value)


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


def test_a_rate_limited_request_is_retried() -> None:
    supabase = uuid4()
    recorder = Recorder(httpx.Response(429), ok(account(supabase)))

    assert recorder.client().create_user(EMAIL).id == supabase
    assert len(recorder.requests) == 2
    assert recorder.slept == [1.0]


def test_retrying_gives_up_rather_than_looping() -> None:
    """A permanently rate-limited project must fail this user and let the run
    continue, not hold the freeze open forever."""
    recorder = Recorder(httpx.Response(429))

    with pytest.raises(AdminApiError):
        recorder.client().create_user(EMAIL)

    assert len(recorder.requests) == MAX_ATTEMPTS


def test_supabase_decides_how_long_to_wait_when_it_says_so() -> None:
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "7"}), ok(account(uuid4())))

    recorder.client().create_user(EMAIL)

    assert recorder.slept == [7.0]


def test_an_absurd_retry_after_is_capped() -> None:
    """A misconfigured proxy answering ``Retry-After: 3600`` would otherwise
    stall the cutover for an hour with no output."""
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "3600"}), ok(account(uuid4())))

    recorder.client().create_user(EMAIL)

    assert recorder.slept == [30.0]


def test_an_unparseable_retry_after_falls_back_to_the_backoff() -> None:
    recorder = Recorder(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        ok(account(uuid4())),
    )

    recorder.client().create_user(EMAIL)

    assert recorder.slept == [1.0]


# --------------------------------------------------------------------------
# the decision, which needs no network at all
# --------------------------------------------------------------------------


def candidate(auth_id: UUID | None = None) -> Candidate:
    return Candidate(user_id=uuid4(), email=EMAIL, auth_id=auth_id)


def test_a_linked_row_is_skipped() -> None:
    assert decide(candidate(uuid4()), None).action is Action.SKIP


def test_a_linked_row_is_skipped_even_when_supabase_holds_a_different_account() -> None:
    """Ordering, and it decides correctness rather than tidiness: preferring
    Supabase's answer would relink a working account to another one."""
    assert decide(candidate(uuid4()), uuid4()).action is Action.SKIP


def test_an_unlinked_row_with_an_existing_account_is_linked() -> None:
    supabase = uuid4()

    plan = decide(candidate(), supabase)

    assert plan.action is Action.LINK
    assert plan.existing_auth_id == supabase


def test_an_unlinked_row_with_no_account_is_created() -> None:
    plan = decide(candidate(), None)

    assert plan.action is Action.CREATE
    assert plan.existing_auth_id is None


def test_the_summary_counts_and_the_failures_name_names() -> None:
    """A count says a run went wrong and nothing about where; the operator's next
    action is to look at each address.

    Separate because they go to different streams — counts to stdout, addresses to
    stderr, matching ``load_identity.py``, so a caller keeping stdout for the
    summary still sees what needs attention.
    """
    outcome = Outcome(created=2, failed=("ada@example.com: 500",))

    assert "failed  1" in outcome.summary()
    assert outcome.failures() == ["FAILED ada@example.com: 500"]


def test_every_user_lands_in_exactly_one_counter() -> None:
    """The counters have to sum to the population the run printed, or an operator
    reconciling "1,200 users, 1,200 provisioned" has a discrepancy with no line
    explaining it."""
    outcome = Outcome(created=3, linked=2, skipped=1, failed=("x: boom",))

    assert outcome.created + outcome.linked + outcome.skipped + len(outcome.failed) == 7


# --------------------------------------------------------------------------
# argument handling — no network, no database
# --------------------------------------------------------------------------


def test_an_address_is_normalised_before_it_reaches_the_database(
    provision_script: ModuleType,
) -> None:
    """``users.email`` carries ``CHECK (email = lower(email))``, so an operator
    typing a capital would otherwise get a constraint violation instead of a
    user — normalisation at the boundary, and this is the boundary."""

    args = provision_script.parse_args(
        ["--create", "--email", " Ada@Example.COM ", "--role", "mentee"]
    )

    assert args.email == "ada@example.com"


def test_a_role_from_the_wrong_vocabulary_is_refused(
    provision_script: ModuleType,
) -> None:
    """``--role super_admin`` on ``--create`` would otherwise reach the enum and
    fail with a bare ``ValueError``."""

    with pytest.raises(SystemExit) as raised:
        provision_script.primary_role("super_admin")

    assert "mentee" in str(raised.value)


def test_a_mode_is_required(
    provision_script: ModuleType,
) -> None:

    with pytest.raises(SystemExit):
        provision_script.parse_args(["--email", "ada@example.com"])


# --------------------------------------------------------------------------
# the lookup, which is where the resume path lives
# --------------------------------------------------------------------------


def listing(paging: PagingRule, *rows: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer like GoTrue, through the shared paging rule in ``conftest``.

    **Honouring ``per_page`` is the whole point.** A private version of this
    ignored it, so a client asking for a single row was served a full page and
    restoring the one-row lookup left the test green. That was the third copy of
    this rule to carry the same omission, and it was written as the fix for the
    first two. There is now one copy.
    """
    return lambda request: paging(list(rows), request)


def test_an_exact_match_behind_a_substring_neighbour_is_found(
    gotrue_paging: PagingRule,
) -> None:
    """``filter`` is a substring search returning newest-first, so the address
    asked for is not necessarily on the first page.

    Asking for one row and reading a neighbour as "no account" is what stranded a
    user permanently: the planner then chose CREATE, Supabase answered
    ``email_exists``, and the run raised — identically on every re-run.
    """
    wanted = uuid4()
    # Newest-first, so a full page of neighbours precedes the address asked for.
    neighbours = [account(uuid4(), f"neighbour{n}@x.com") for n in range(LOOKUP_PAGE_SIZE)]
    recorder = Recorder(answer=listing(gotrue_paging, *neighbours, account(wanted, "ada@x.com")))

    found = recorder.client().find_by_email("ada@x.com")

    assert found is not None
    assert found.id == wanted
    # Two pages, because one was not enough — the assertion that dies if the
    # client goes back to asking for a single row.
    assert len(recorder.requests) == 2


def test_a_short_page_ends_the_walk() -> None:
    """The ordinary case is one call. Paging must not turn every lookup into
    twenty."""
    recorder = Recorder(ok({"users": [account(uuid4())]}))

    recorder.client().find_by_email(EMAIL)

    assert len(recorder.requests) == 1


def test_a_filter_that_never_narrows_is_refused_loudly() -> None:
    """If the Admin API ignored ``filter`` altogether, walking a large project
    would never terminate. It stops and raises, because a wrong ``None`` is read
    upstream as "no account exists" and creates a duplicate."""
    full = [account(uuid4(), f"other{n}@x.com") for n in range(LOOKUP_PAGE_SIZE)]
    recorder = Recorder(answer=lambda _: ok({"users": full}))

    with pytest.raises(AdminApiError):
        recorder.client().find_by_email(EMAIL)

    assert len(recorder.requests) == MAX_LOOKUP_PAGES


def test_an_address_supabase_says_exists_but_cannot_produce_is_refused_clearly() -> None:
    """Supabase and our own lookup contradicting each other is not "create failed
    with 422" — the next action is to look at the Admin API, not at the user."""
    recorder = Recorder(ok({"error_code": "email_exists"}, status=422), ok({"users": []}))

    with pytest.raises(AdminApiError) as raised:
        recorder.client().create_user(EMAIL)

    assert "already" in str(raised.value).lower()


def test_a_negative_retry_after_does_not_become_a_crash() -> None:
    """``time.sleep(-5)`` raises, and the per-user handler would record a
    provisioning failure for someone who only needed to wait."""
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "-5"}), ok(account(uuid4())))

    recorder.client().create_user(EMAIL)

    assert recorder.slept == [0.0]


# --------------------------------------------------------------------------
# configuration and arguments
# --------------------------------------------------------------------------


def test_provisioning_without_a_service_role_key_names_the_variable(
    provision_script: ModuleType,
) -> None:

    with pytest.raises(ConfigurationError) as raised:
        provision_script.build_client(Settings(_env_file=None), httpx.Client())

    assert "SERVICE_ROLE_KEY" in str(raised.value)


def test_flags_a_mode_ignores_are_refused_rather_than_dropped(
    provision_script: ModuleType,
) -> None:
    """``--verify --email ada@example.com`` reads as "verify this one user". It
    does not, and silently ignoring the flag is how an operator believes it did."""

    with pytest.raises(SystemExit):
        provision_script.parse_args(["--verify", "--email", "ada@example.com"])
