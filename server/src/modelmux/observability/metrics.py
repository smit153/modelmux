"""Optional Prometheus metrics (MODELMUX_ENABLE_METRICS, off by default).

Labels are low-cardinality only: outcome codes and the public model ID
(bounded by the allowlist). Never prompts, keys or request data.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from modelmux.api.auth import require_api_key


class Metrics:
    def __init__(self, *, active: Callable[[], int], waiting: Callable[[], int]) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "modelmux_chat_requests",
            "Chat completion requests by model and outcome code.",
            ["model", "outcome"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "modelmux_chat_request_duration_seconds",
            "Chat completion wall time.",
            ["model"],
            buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
            registry=self.registry,
        )
        active_gauge = Gauge(
            "modelmux_active_processes", "CLI processes running.", registry=self.registry
        )
        active_gauge.set_function(lambda: float(active()))
        waiting_gauge = Gauge(
            "modelmux_queued_requests", "Requests waiting for a slot.", registry=self.registry
        )
        waiting_gauge.set_function(lambda: float(waiting()))

    def observe(self, model: str, outcome: str, seconds: float) -> None:
        self.requests.labels(model=model, outcome=outcome).inc()
        self.duration.labels(model=model).observe(seconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)


router = APIRouter()


@router.get("/metrics", dependencies=[Depends(require_api_key)], include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    metrics: Metrics = request.app.state.metrics
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)
