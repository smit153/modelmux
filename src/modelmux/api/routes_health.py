"""Unauthenticated liveness and readiness probes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@dataclass
class Readiness:
    """Readiness inputs. The driver probe and the runtime limiter update this.

    ``reason`` values are short, fixed, machine-readable strings; never
    internal details.
    """

    driver_ready: bool = False
    saturated: Callable[[], bool] = field(default=lambda: False)

    def reason(self) -> str | None:
        if not self.driver_ready:
            return "driver_not_ready"
        if self.saturated():
            return "saturated"
        return None


@router.get("/health/live", include_in_schema=False)
async def live() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/health/ready", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    readiness: Readiness = request.app.state.readiness
    reason = readiness.reason()
    if reason is not None:
        return JSONResponse({"status": "not_ready", "reason": reason}, status_code=503)
    return JSONResponse({"status": "ready"})
