"""Which push endpoints this platform will ever POST to (#386, decision 312)."""

import pytest

from app.domain.push import ENDPOINT_MAX_LENGTH, PUSH_SERVICE_HOSTS, is_push_service


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/dXk2:APA91b",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAAA",
        "https://web.push.apple.com/QGuQ-yfFeyc",
        "https://wns2-by3p.notify.windows.com/w/?token=BQYAAA%3d%3d",
        "https://FCM.googleapis.com/fcm/send/abc",
    ],
)
def test_every_browsers_push_service_is_allowed(endpoint: str) -> None:
    assert is_push_service(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/push",
        "https://internal.railway.internal/push",
        "https://fcm.googleapis.com.evil.test/fcm/send/abc",
        "https://evilfcm.googleapis.com/fcm/send/abc",
        "https://googleapis.com/fcm/send/abc",
        "https://user:pw@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://fcm.googleapis.com:notaport/fcm/send/abc",
        "https://fcm.googleapis.com/fcm/send/a b",
        "https://fcm.googleapis.com/fcm/send/é",
        "https://",
        "fcm.googleapis.com/fcm/send/abc",
        # Hosts a different URL parser would read differently.
        r"https://x\.fcm.googleapis.com/a",
        "https://127.0.0.1%2f.fcm.googleapis.com/a",
        "https://a..fcm.googleapis.com/a",
        "https://_x.fcm.googleapis.com/a",
        "https://fcm.googleapis.com/" + "a" * ENDPOINT_MAX_LENGTH,
    ],
)
def test_anything_else_is_refused(endpoint: str) -> None:
    assert not is_push_service(endpoint)


def test_the_longest_allowed_endpoint_is_exactly_the_bound() -> None:
    prefix = "https://fcm.googleapis.com/"
    assert is_push_service(prefix + "a" * (ENDPOINT_MAX_LENGTH - len(prefix)))
    assert not is_push_service(prefix + "a" * (ENDPOINT_MAX_LENGTH - len(prefix) + 1))


def test_every_listed_host_is_a_bare_lowercase_name() -> None:
    for host in PUSH_SERVICE_HOSTS:
        assert host == host.lower().strip(".")
        assert "/" not in host
        assert ":" not in host
