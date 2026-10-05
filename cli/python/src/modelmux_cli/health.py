"""Talk to a running ModelMux server over HTTP (localhost only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

LOCALHOST = "127.0.0.1"


def base_url(port: int) -> str:
    return f"http://{LOCALHOST}:{port}"


def get_json(
    port: int, path: str, *, timeout: float = 3.0, api_key: str | None = None
) -> tuple[int, Any]:
    """GET ``path`` on the local server. Returns (status, body); status 0 if unreachable."""
    request = urllib.request.Request(base_url(port) + path)  # noqa: S310 - fixed http://127.0.0.1
    if api_key is not None:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"null")
        except ValueError:
            body = None
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def ready(port: int) -> bool:
    status, body = get_json(port, "/health/ready")
    return status == 200 and isinstance(body, dict) and body.get("status") == "ready"


def wait_until(
    check: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 1.0,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll ``check`` until it is true (True), ``should_stop`` is true or time runs out (False)."""
    deadline = clock() + timeout
    while True:
        if check():
            return True
        if should_stop() or clock() >= deadline:
            return False
        sleep(interval)
