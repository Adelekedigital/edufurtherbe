"""Web Push's VAPID settings: all three or none, each in its shape (#386)."""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.core.config import Settings

SUBJECT = "mailto:ops@example.test"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def vapid_pair() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(public), _b64(key.private_numbers().private_value.to_bytes(32, "big"))


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_unset_means_push_is_off() -> None:
    assert not settings().push_configured


def test_blank_values_mean_unset() -> None:
    assert not settings(
        vapid_public_key=" ", vapid_private_key="", vapid_subject=""
    ).push_configured


def test_a_full_pair_and_subject_turn_push_on() -> None:
    public, private = vapid_pair()

    configured = settings(vapid_public_key=public, vapid_private_key=private, vapid_subject=SUBJECT)

    assert configured.push_configured
    assert configured.vapid_public_key == public


def test_a_padded_key_and_an_https_subject_are_accepted() -> None:
    public, private = vapid_pair()

    assert settings(
        vapid_public_key=public + "=",
        vapid_private_key=private + "=",
        vapid_subject="https://edufurther.example",
    ).push_configured


@pytest.mark.parametrize("missing", ["vapid_public_key", "vapid_private_key", "vapid_subject"])
def test_any_one_missing_stops_startup(missing: str) -> None:
    public, private = vapid_pair()
    values = {"vapid_public_key": public, "vapid_private_key": private, "vapid_subject": SUBJECT}
    del values[missing]

    with pytest.raises(ValidationError, match="go together"):
        settings(**values)


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("vapid_public_key", "not base64!", "VAPID_PUBLIC_KEY"),
        ("vapid_public_key", _b64(b"\x04" + b"\x01" * 63), "VAPID_PUBLIC_KEY"),
        ("vapid_public_key", _b64(b"\x02" + b"\x01" * 64), "VAPID_PUBLIC_KEY"),
        ("vapid_private_key", _b64(b"\x01" * 31), "VAPID_PRIVATE_KEY"),
        ("vapid_private_key", "a+b/c", "VAPID_PRIVATE_KEY"),
        ("vapid_private_key", _b64(bytes([0xFF]) * 32), "VAPID_PUBLIC_KEY is not the public half"),
        ("vapid_subject", "ops@example.test", "VAPID_SUBJECT"),
        ("vapid_subject", "http://edufurther.example", "VAPID_SUBJECT"),
    ],
)
def test_a_malformed_value_stops_startup(field: str, bad: str, message: str) -> None:
    public, private = vapid_pair()
    values = {"vapid_public_key": public, "vapid_private_key": private, "vapid_subject": SUBJECT}
    values[field] = bad

    with pytest.raises(ValidationError, match=message):
        settings(**values)


def test_a_public_key_from_another_pair_stops_startup() -> None:
    public, _ = vapid_pair()
    _, private = vapid_pair()

    with pytest.raises(ValidationError, match="not the public half"):
        settings(vapid_public_key=public, vapid_private_key=private, vapid_subject=SUBJECT)
