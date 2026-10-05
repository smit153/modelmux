"""Inbound API key authentication (``Authorization: Bearer <key>``)."""

from __future__ import annotations

import hmac
import itertools
import logging

from fastapi import Request

from modelmux.errors import AuthenticationFailedError

log = logging.getLogger("modelmux.auth")

NO_AUTH_WARNING_EVERY = 1000


class Authenticator:
    """Checks bearer tokens in constant time against every configured key."""

    def __init__(self, keys: tuple[str, ...], *, allow_no_auth: bool = False) -> None:
        self._keys = tuple(key.encode() for key in keys)
        self._allow_no_auth = allow_no_auth and not keys
        self._counter = itertools.count(1)

    @property
    def enabled(self) -> bool:
        return not self._allow_no_auth

    def warn_if_disabled(self) -> None:
        if not self.enabled:
            log.warning(
                "authentication is DISABLED (MODELMUX_ALLOW_NO_AUTH=true); "
                "anyone who can reach this port can use the backend",
                extra={"event": "auth_disabled"},
            )

    def check(self, authorization: str | None) -> None:
        """Raise ``AuthenticationFailedError`` unless the header holds a valid key."""
        if not self.enabled:
            if next(self._counter) % NO_AUTH_WARNING_EVERY == 0:
                self.warn_if_disabled()
            return

        token = b""
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                token = value.strip().encode("utf-8", "replace")

        # Compare against every key; no early exit, so timing does not reveal
        # which key (if any) matched.
        matched = False
        for key in self._keys:
            matched |= hmac.compare_digest(token, key)
        if not token or not matched:
            raise AuthenticationFailedError(
                "missing bearer token" if not token else "bearer token did not match"
            )


async def require_api_key(request: Request) -> None:
    """FastAPI dependency guarding authenticated routes."""
    authenticator: Authenticator = request.app.state.authenticator
    authenticator.check(request.headers.get("authorization"))
