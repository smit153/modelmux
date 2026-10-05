"""``python -m modelmux``: run the server with the settings from the environment.

Always one worker (limits are per process; scale with more containers), no
``server`` header, and proxy headers only from ``MODELMUX_TRUSTED_PROXIES``.
"""

from __future__ import annotations

import sys
from typing import Any

import uvicorn

from modelmux.config import ConfigError, Settings, load_settings


def uvicorn_options(settings: Settings) -> dict[str, Any]:
    trusted = ",".join(settings.trusted_proxies)
    return {
        "host": settings.host,
        "port": settings.port,
        "workers": 1,
        "server_header": False,
        "proxy_headers": bool(trusted),
        "forwarded_allow_ips": trusted or None,
        "log_config": None,  # ModelMux configures JSON logging itself
        "access_log": False,  # ModelMux writes its own access log
    }


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"modelmux: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run("modelmux.main:app", **uvicorn_options(settings))


if __name__ == "__main__":
    main()
