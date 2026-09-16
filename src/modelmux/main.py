"""Application factory.

Run with ``uvicorn modelmux.main:app --workers 1``. Exactly one worker: the
concurrency limits are per process, so more workers would multiply them.
Scale out by running more containers instead.

Startup is fail-fast: invalid configuration, a missing or unsafe CLI binary,
an unsupported CLI version, a missing lockdown flag, or a failed live check
all stop the process with a clear message (no "start not ready and retry").
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from modelmux.core.pipeline import Pipeline
from modelmux.drivers.base import Driver
from modelmux.drivers.registry import (
    DriverLoadError,
    create_driver,
    load_driver_class,
    resolve_models,
)
from modelmux.observability.logging import setup_logging
from modelmux.runtime.limits import ConcurrencyLimiter
from modelmux.runtime.runner import (
    BinaryResolutionError,
    ProbeContext,
    RunLimits,
    Runner,
    resolve_binary,
)
from modelmux.runtime.workspace import WorkRootError, prepare_work_root

log = logging.getLogger("modelmux")


class StartupError(Exception):
    """ModelMux cannot start. The message is safe to print (no secrets)."""


def _build_driver(settings: Settings) -> Driver:
    try:
        cls = load_driver_class(settings.driver)
        binary = resolve_binary(cls.binary_name, settings.cli_path)
        return create_driver(cls, binary)
    except (DriverLoadError, BinaryResolutionError) as exc:
        raise StartupError(str(exc)) from None


async def _startup_checks(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    driver: Driver = app.state.driver
    try:
        prepare_work_root(settings.work_root)
    except (WorkRootError, OSError) as exc:
        raise StartupError(f"MODELMUX_WORK_ROOT is unusable: {exc}") from None
    result = await driver.probe(ProbeContext(app.state.runner, settings.work_root))
    if not result.ok:
        log.error(
            "driver probe failed",
            extra={"event": "probe_failed", "reason": result.reason, "version": result.version},
        )
        raise StartupError(f"driver {driver.name!r} probe failed: {result.reason}")
    app.state.readiness.driver_ready = True
    log.info("driver ready", extra={"event": "probe_ok", "version": result.version})


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _startup_checks(app)
    yield


def create_app(
    settings: Settings | None = None, *, extra_env_allowlist: frozenset[str] = frozenset()
) -> FastAPI:
    """Build the ModelMux ASGI app.

    Raises ``ConfigError``/``StartupError``. The driver probe runs in the
    lifespan startup, so a failed probe stops the server before it serves.
    ``extra_env_allowlist`` exists for tests only; it is not configurable.
    """
    if settings is None:
        settings = load_settings()

    setup_logging(
        settings.log_level,
        secrets=settings.api_key_values(),
        log_content=settings.log_content,
    )

    driver = _build_driver(settings)
    try:
        models = resolve_models(driver, settings.models)
    except DriverLoadError as exc:
        raise StartupError(str(exc)) from None

    runner = Runner(
        driver.binary,
        home=settings.driver_home,
        env_allowlist=driver.env_allowlist() | extra_env_allowlist,
        limits=RunLimits.from_settings(settings),
    )
    limiter = ConcurrencyLimiter(
        settings.max_concurrent_processes, settings.max_queue_size, settings.queue_timeout
    )

    docs = settings.enable_docs
    app = FastAPI(
        title="ModelMux",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.authenticator = Authenticator(
        settings.api_key_values(), allow_no_auth=settings.allow_no_auth
    )
    app.state.driver = driver
    app.state.runner = runner
    app.state.limiter = limiter
    app.state.pipeline = Pipeline(
        driver=driver, runner=runner, limiter=limiter, models=models, settings=settings
    )
    app.state.readiness = Readiness(saturated=limiter.saturated)

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
    app.add_middleware(RequestContextMiddleware, driver_name=driver.name)

    app.state.authenticator.warn_if_disabled()
    log.info(
        "modelmux configured",
        extra={
            "event": "startup",
            "version": __version__,
            "driver_name": driver.name,
            "models": len(models),
        },
    )
    return app


def _app_from_env() -> FastAPI:
    try:
        return create_app()
    except (ConfigError, StartupError) as exc:
        # Messages name settings and causes but never secret values.
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
