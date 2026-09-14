"""Application factory.

Run with ``uvicorn modelmux.main:app --workers 1``. Exactly one worker: the
concurrency limits are per process, so more workers would multiply them.
Scale out by running more containers instead.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from modelmux import __version__
from modelmux.api.auth import Authenticator
from modelmux.api.handlers import register_exception_handlers
from modelmux.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from modelmux.api.routes_health import Readiness
from modelmux.api.routes_health import router as health_router
from modelmux.config import ConfigError, Settings, load_settings
from modelmux.observability.logging import setup_logging

log = logging.getLogger("modelmux")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ModelMux ASGI app. Raises ``ConfigError`` on invalid settings."""
    if settings is None:
        settings = load_settings()

    setup_logging(
        settings.log_level,
        secrets=settings.api_key_values(),
        log_content=settings.log_content,
    )

    docs = settings.enable_docs
    app = FastAPI(
        title="ModelMux",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.authenticator = Authenticator(
        settings.api_key_values(), allow_no_auth=settings.allow_no_auth
    )
    app.state.readiness = Readiness()

    register_exception_handlers(app)
    app.include_router(health_router)

    # Middleware added last runs first: request context wraps everything.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware, driver_name=settings.driver)

    app.state.authenticator.warn_if_disabled()
    log.info(
        "modelmux configured",
        extra={"event": "startup", "version": __version__, "driver_name": settings.driver},
    )
    return app


def _app_from_env() -> FastAPI:
    try:
        return create_app()
    except ConfigError as exc:
        # The message names settings but never their values.
        print(f"modelmux: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build ``app`` lazily, so importing this module never requires configuration."""
    global _app  # noqa: PLW0603 - module-level singleton for uvicorn
    if name == "app":
        if _app is None:
            _app = _app_from_env()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
