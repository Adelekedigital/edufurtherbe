"""What makes two requests the same request.

Unit rather than integration because the answer is a product rule, not a storage
detail: whether reordering a JSON object changes a booking is a decision, and it
has to be assertable without a database.
"""

from __future__ import annotations

from app.domain.idempotency import request_fingerprint

ENDPOINT = "POST /api/v1/sessions"


def test_field_order_does_not_change_the_request() -> None:
    """**The one that would break a naive implementation.**

    JSON object order is not semantic. Two clients — or one client on two
    platforms — serialising the same booking in different field orders must not
    look like different requests, because the mismatch answer is a `422` telling
    an honest retry that it reused a key wrongly.
    """
    one = request_fingerprint(ENDPOINT, {"session_type_id": "a", "starts_at": "b"})
    other = request_fingerprint(ENDPOINT, {"starts_at": "b", "session_type_id": "a"})

    assert one == other


def test_a_changed_value_is_a_different_request() -> None:
    """The positive case, and the whole point: a client reusing a key for a
    different booking must be told, not silently handed the first answer."""
    assert request_fingerprint(ENDPOINT, {"starts_at": "09:00"}) != request_fingerprint(
        ENDPOINT, {"starts_at": "10:00"}
    )


def test_the_endpoint_is_part_of_the_request() -> None:
    """A key reused across two endpoints is a mistake, and folding the endpoint
    in turns it into a mismatch rather than a replay that answers a booking with
    somebody else's write."""
    body = {"session_type_id": "a"}

    assert request_fingerprint(ENDPOINT, body) != request_fingerprint("POST /api/v1/other", body)


def test_absent_and_null_are_different() -> None:
    """Deliberate, and stated because the obvious implementations disagree.

    To a PATCH-shaped body `{"topic": null}` and `{}` mean different things —
    clear it versus leave it — so a fingerprint that conflated them would let a
    genuinely different request replay the first one's answer. Booking is a POST
    where the two happen to coincide; the rule is here because the function is
    not.
    """
    assert request_fingerprint(ENDPOINT, {"topic": None}) != request_fingerprint(ENDPOINT, {})


def test_it_is_stable_across_calls() -> None:
    """A hash that varied per process would make every retry a mismatch, and
    only after a deploy — which is the kind of bug that looks like a client
    fault."""
    body = {"session_type_id": "a", "starts_at": "2026-01-01T09:00:00Z", "topic": None}

    assert request_fingerprint(ENDPOINT, body) == request_fingerprint(ENDPOINT, body)
