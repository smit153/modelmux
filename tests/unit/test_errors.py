from __future__ import annotations

import pytest

from modelmux import errors as e

# class, code, http, openai type, retryable -- the table from the plan, section 13.1
TABLE: list[tuple[type[e.ModelMuxError], str, int, str, bool]] = [
    (e.InvalidRequestError, "invalid_request", 400, "invalid_request_error", False),
    (e.UnsupportedParameterError, "unsupported_parameter", 400, "invalid_request_error", False),
    (e.UnsupportedContentError, "unsupported_content", 400, "invalid_request_error", False),
    (e.AuthenticationFailedError, "authentication_failed", 401, "authentication_error", False),
    (e.ModelNotFoundError, "model_not_found", 404, "invalid_request_error", False),
    (e.UnsupportedMediaTypeError, "unsupported_media_type", 415, "invalid_request_error", False),
    (e.PayloadTooLargeError, "payload_too_large", 413, "invalid_request_error", False),
    (e.ContextTooLargeError, "context_too_large", 413, "invalid_request_error", False),
    (e.ProviderRateLimitedError, "provider_rate_limited", 429, "rate_limit_error", True),
    (e.OverloadedError, "overloaded", 503, "server_error", True),
    (e.ProviderUnavailableError, "provider_unavailable", 503, "server_error", True),
    (e.ProviderAuthError, "provider_auth_error", 502, "server_error", False),
    (e.ProviderError, "provider_error", 502, "server_error", True),
    (e.ProtocolError, "protocol_error", 502, "server_error", True),
    (e.InvalidModelOutputError, "invalid_model_output", 502, "server_error", True),
    (e.OutputTooLargeError, "output_too_large", 502, "server_error", False),
    (e.SandboxViolationError, "sandbox_violation", 502, "server_error", False),
    (e.ProviderTimeoutError, "provider_timeout", 504, "server_error", True),
    (e.DriverNotReadyError, "driver_not_ready", 503, "server_error", True),
    (e.InternalError, "internal_error", 500, "server_error", False),
]


@pytest.mark.parametrize(("cls", "code", "status", "otype", "retryable"), TABLE)
def test_error_table(
    cls: type[e.ModelMuxError], code: str, status: int, otype: str, retryable: bool
) -> None:
    err = cls()
    assert err.code == code
    assert err.http_status == status
    assert err.openai_type == otype
    assert err.retryable is retryable
    assert err.public_message


@pytest.mark.parametrize("cls", [row[0] for row in TABLE])
def test_internal_detail_never_rendered(cls: type[e.ModelMuxError]) -> None:
    err = cls("stderr: /secret/path argv=--foo SENTINEL")
    body = err.to_openai()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"message", "type", "code", "param"}
    assert "SENTINEL" not in str(body)
    assert "SENTINEL" in str(err)  # logs do get it


def test_codes_are_unique() -> None:
    codes = [row[1] for row in TABLE]
    assert len(codes) == len(set(codes))


def test_message_and_param_override() -> None:
    err = e.InvalidRequestError(message="messages.0.role is invalid", param="messages.0.role")
    assert err.to_openai()["error"] == {
        "message": "messages.0.role is invalid",
        "type": "invalid_request_error",
        "code": "invalid_request",
        "param": "messages.0.role",
    }


def test_retry_after_header() -> None:
    assert e.OverloadedError(retry_after=2.4).headers() == {"Retry-After": "2"}
    assert e.OverloadedError(retry_after=0.1).headers() == {"Retry-After": "1"}
    assert e.OverloadedError().headers() == {}
