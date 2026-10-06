"""Test fakes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

FAKE_CLI = Path(__file__).with_name("fake_cli.py")

# Env vars the fake CLI reads. Tests add these to the driver allowlist only.
FAKE_ENV_KEYS = frozenset(
    {"FAKE_SCENARIO", "FAKE_OUT", "FAKE_SIZE", "FAKE_FIXTURE", "FAKE_DELAY", "FAKE_EXIT",
     "FAKE_VERSION", "FAKE_UNKNOWN_FLAG", "FAKE_PROBE", "FAKE_REPLY", "FAKE_NO_USAGE",
     "FAKE_HANG_AFTER", "FAKE_UNKNOWN_FEATURE", "FAKE_UNKNOWN_KEY",
     "FAKE_REPLIES", "FAKE_COUNTER", "FAKE_PROBE_OUT", "FAKE_MODEL_QUERY", "FAKE_MODEL_LIST",
     "FAKE_DEFAULT_MODEL", "FAKE_MODEL_LOG", "FAKE_CATALOG"}
)  # fmt: skip


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def install_fake_cli(directory: Path, name: str = "fake-cli") -> Path:
    """Write an executable wrapper that runs the fake CLI with this interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / name
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(FAKE_CLI)!r}\n"
        f"runpy.run_path({str(FAKE_CLI)!r}, init_globals={{'FAKE_NAME': {name!r}}},"
        " run_name='__main__')\n"
    )
    os.chmod(wrapper, 0o700)
    return wrapper
