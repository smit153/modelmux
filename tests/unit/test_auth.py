from __future__ import annotations

import io

import pytest

from modelmux.api import auth as auth_module
from modelmux.api.auth import NO_AUTH_WARNING_EVERY, Authenticator
from modelmux.errors import AuthenticationFailedError
from modelmux.observability.logging import setup_logging
from tests.conftest import OTHER_API_KEY, TEST_API_KEY


@pytest.fixture
def auth() -> Authenticator:
    return Authenticator((TEST_API_KEY, OTHER_API_KEY))


@pytest.mark.parametrize("key", [TEST_API_KEY, OTHER_API_KEY])
def test_valid_keys(auth: Authenticator, key: str) -> None:
    auth.check(f"Bearer {key}")
    auth.check(f"bearer {key}")


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        f"Basic {TEST_API_KEY}",
        TEST_API_KEY,
        f"Bearer {TEST_API_KEY}x",
        f"Bearer {TEST_API_KEY[:-1]}",
        "Bearer wrong-key-" + "c" * 40,
        "Bearer ünïcödé",
    ],
)
def test_invalid(auth: Authenticator, header: str | None) -> None:
    with pytest.raises(AuthenticationFailedError) as info:
        auth.check(header)
    assert TEST_API_KEY not in str(info.value.to_openai())


def test_compares_every_key_without_early_exit(
    auth: Authenticator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = auth_module.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)
    auth.check(f"Bearer {TEST_API_KEY}")  # matches the FIRST key
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(AuthenticationFailedError):
        auth.check("Bearer nope")
    assert len(calls) == 2


def test_no_auth_mode_warns_periodically() -> None:
    stream = io.StringIO()
    setup_logging("INFO", stream=stream)
    try:
        auth = Authenticator((), allow_no_auth=True)
        assert not auth.enabled
        for _ in range(NO_AUTH_WARNING_EVERY * 2):
            auth.check(None)
        assert stream.getvalue().count("auth_disabled") == 2
    finally:
        setup_logging("INFO", stream=io.StringIO())


def test_allow_no_auth_ignored_when_keys_set() -> None:
    auth = Authenticator((TEST_API_KEY,), allow_no_auth=True)
    assert auth.enabled
    with pytest.raises(AuthenticationFailedError):
        auth.check(None)
