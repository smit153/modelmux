"""Is a local port free? (Checked with a socket bind: no extra processes.)"""

from __future__ import annotations

import socket
import sys

from modelmux_cli.health import LOCALHOST


def port_free(port: int, host: str = LOCALHOST) -> bool:
    """True unless another program is listening on ``host:port``.

    Ports in TIME_WAIT (for example right after ``modelmux down``) count as
    free, as they do for Docker: on POSIX ``SO_REUSEADDR`` ignores TIME_WAIT
    but still fails against a live listener. On Windows ``SO_REUSEADDR`` would
    allow stealing a port, so ``SO_EXCLUSIVEADDRUSE`` is used instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE")  # noqa: B009 - Windows-only name
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True
