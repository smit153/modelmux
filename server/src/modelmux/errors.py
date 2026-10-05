"""The ModelMux error hierarchy.

Every failure the service can report is a ``ModelMuxError`` subclass. Each
class fixes the error code, HTTP status, OpenAI error type, retryability, and
a public message that is safe to return. ``internal_detail`` is for logs only
and is never sent to clients.
"""

from __future__ import annotations

from typing import Any, ClassVar

INVALID_REQUEST = "invalid_request_error"
AUTHENTICATION = "authentication_error"
RATE_LIMIT = "rate_limit_error"
SERVER = "server_error"


class ModelMuxError(Exception):
    """Base class for all errors rendered to clients."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500
    openai_type: ClassVar[str] = SERVER
    retryable: ClassVar[bool] = False
    public_message: ClassVar[str] = "An internal error occurred."

    def __init__(
        self,
        internal_detail: str | None = None,
        *,
        message: str | None = None,
        param: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Create the error.

        ``message`` overrides the public message and must be safe to show to
        clients (no request values, paths, argv, env or stderr).
        """
        self.internal_detail = internal_detail
        self.message = message or self.public_message
        self.param = param
        self.retry_after = retry_after
        super().__init__(self.code)

    def __str__(self) -> str:
        if self.internal_detail:
            return f"{self.code}: {self.internal_detail}"
        return self.code

    def to_openai(self) -> dict[str, Any]:
        """The OpenAI-shaped error body."""
        return {
            "error": {
                "message": self.message,
                "type": self.openai_type,
                "code": self.code,
                "param": self.param,
            }
        }

    def headers(self) -> dict[str, str]:
        """Extra response headers for this error."""
        if self.retry_after is not None:
            return {"Retry-After": str(max(1, round(self.retry_after)))}
        return {}


# ---------------------------------------------------------------- 4xx


class InvalidRequestError(ModelMuxError):
    code = "invalid_request"
    http_status = 400
    openai_type = INVALID_REQUEST
    public_message = "The request is invalid."


class UnsupportedParameterError(ModelMuxError):
    code = "unsupported_parameter"
    http_status = 400
    openai_type = INVALID_REQUEST
    public_message = "The request uses a parameter that is not supported."


class UnsupportedContentError(ModelMuxError):
    code = "unsupported_content"
    http_status = 400
    openai_type = INVALID_REQUEST
    public_message = "Only text content is supported."


class AuthenticationFailedError(ModelMuxError):
    code = "authentication_failed"
    http_status = 401
    openai_type = AUTHENTICATION
    public_message = "Invalid or missing API key."


class ModelNotFoundError(ModelMuxError):
    code = "model_not_found"
    http_status = 404
    openai_type = INVALID_REQUEST
    public_message = "The requested model does not exist or is not available."


class NotFoundError(ModelMuxError):
    code = "not_found"
    http_status = 404
    openai_type = INVALID_REQUEST
    public_message = "Not found."


class MethodNotAllowedError(ModelMuxError):
    code = "method_not_allowed"
    http_status = 405
    openai_type = INVALID_REQUEST
    public_message = "Method not allowed."


class PayloadTooLargeError(ModelMuxError):
    code = "payload_too_large"
    http_status = 413
    openai_type = INVALID_REQUEST
    public_message = "The request is too large."


class ContextTooLargeError(ModelMuxError):
    code = "context_too_large"
    http_status = 413
    openai_type = INVALID_REQUEST
    public_message = "The conversation is too long for this model."


class UnsupportedMediaTypeError(ModelMuxError):
    code = "unsupported_media_type"
    http_status = 415
    openai_type = INVALID_REQUEST
    public_message = "Content-Type must be application/json."


class ProviderRateLimitedError(ModelMuxError):
    code = "provider_rate_limited"
    http_status = 429
    openai_type = RATE_LIMIT
    retryable = True
    public_message = "The upstream provider rate limit was reached. Retry later."


# ---------------------------------------------------------------- 5xx


class InternalError(ModelMuxError):
    """Any unexpected exception."""


class BadGatewayError(ModelMuxError):
    """Upstream (CLI or provider) failures."""

    http_status = 502
    openai_type = SERVER


class ProviderAuthError(BadGatewayError):
    code = "provider_auth_error"
    public_message = "The upstream provider rejected the configured credentials."


class ProviderError(BadGatewayError):
    code = "provider_error"
    retryable = True
    public_message = "The upstream model failed to produce a response."


class ProtocolError(BadGatewayError):
    code = "protocol_error"
    retryable = True
    public_message = "The upstream model returned an unreadable response."


class InvalidModelOutputError(BadGatewayError):
    code = "invalid_model_output"
    retryable = True
    public_message = "The model output did not match the requested format."


class OutputTooLargeError(BadGatewayError):
    code = "output_too_large"
    public_message = "The model output exceeded the size limit."


class SandboxViolationError(BadGatewayError):
    code = "sandbox_violation"
    public_message = "The model attempted a forbidden action and the request was stopped."


class OverloadedError(ModelMuxError):
    code = "overloaded"
    http_status = 503
    openai_type = SERVER
    retryable = True
    public_message = "The server is at capacity. Retry later."


class ProviderUnavailableError(ModelMuxError):
    code = "provider_unavailable"
    http_status = 503
    openai_type = SERVER
    retryable = True
    public_message = "The upstream provider is unavailable. Retry later."


class DriverNotReadyError(ModelMuxError):
    code = "driver_not_ready"
    http_status = 503
    openai_type = SERVER
    retryable = True
    public_message = "The model backend is not ready."


class ProviderTimeoutError(ModelMuxError):
    code = "provider_timeout"
    http_status = 504
    openai_type = SERVER
    retryable = True
    public_message = "The upstream model timed out."
