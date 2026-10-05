"""The server image this CLI release runs, pinned by digest.

Rewritten by the release workflow (``ghcr.io/smit153/modelmux@sha256:...``).
``None`` in development builds: pass ``--image`` instead.
"""

IMAGE: str | None = None
