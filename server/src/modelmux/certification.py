"""Build-time certification of the pinned CLIs, and its check at startup.

``python -m modelmux certify`` runs every driver's ``certify()`` inside the
image build and writes a manifest (``/opt/modelmux/manifest.json``). At
startup the server only checks that the manifest belongs to this exact
installation: the hashes of the driver's integrity files and of its
lockdown spec must match. Anything else fails closed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modelmux import __version__
from modelmux.config import validate_cli_model, validate_model_id
from modelmux.drivers.base import Driver, ModelInfo
from modelmux.drivers.registry import BUILTIN_DRIVERS, DriverLoadError, load_driver_class
from modelmux.runtime.runner import (
    BinaryResolutionError,
    ProbeContext,
    RunLimits,
    Runner,
    resolve_binary,
)
from modelmux.runtime.workspace import prepare_work_root

SCHEMA_VERSION = 1
DEFAULT_MANIFEST = Path("/opt/modelmux/manifest.json")
MAX_MANIFEST_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024


class CertificationError(Exception):
    """Certification failed. The message is safe to print."""


class ManifestError(Exception):
    """The manifest is missing, unreadable, or not for this installation."""


@dataclass(frozen=True)
class Certificate:
    """What certification established for one driver."""

    driver: str
    cli_version: str
    files: dict[str, str]  # file name -> sha256
    lockdown: str  # sha256 of the lockdown spec
    models: tuple[ModelInfo, ...] | None  # None: discovered at startup


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def lockdown_sha256(driver: Driver) -> str:
    spec = json.dumps(list(driver.lockdown_spec()), ensure_ascii=False)
    return hashlib.sha256(spec.encode()).hexdigest()


def file_hashes(driver: Driver) -> dict[str, str]:
    """sha256 of each integrity file, keyed by file name (paths differ per install)."""
    hashes: dict[str, str] = {}
    for path in driver.integrity_files():
        resolved = path.resolve(strict=True)
        hashes[resolved.name] = file_sha256(resolved)
    return hashes


# ---------------------------------------------------------------- certify (build)


async def certify_driver(
    driver: Driver, *, source_env: dict[str, str] | None = None
) -> Certificate:
    """Run the driver's build-time checks in a throwaway home and work root.

    ``source_env`` is for tests (it lets the fake CLI's knobs through).
    """
    with tempfile.TemporaryDirectory(prefix="modelmux-certify-") as tmp:
        home = Path(tmp) / "home"
        home.mkdir(mode=0o700)
        work_root = Path(tmp) / "work"
        prepare_work_root(work_root)
        allowlist = driver.env_allowlist() | frozenset(source_env or {})
        runner = Runner(
            driver.binary, home=home, env_allowlist=allowlist, limits=RunLimits(),
            source_env=source_env,
        )  # fmt: skip
        result = await driver.certify(ProbeContext(runner, work_root))
    if not result.ok or result.version is None:
        raise CertificationError(f"driver {driver.name!r} failed certification: {result.reason}")
    return Certificate(
        driver=driver.name,
        cli_version=result.version,
        files=file_hashes(driver),
        lockdown=lockdown_sha256(driver),
        models=result.models,
    )


def manifest_json(certificates: list[Certificate]) -> str:
    drivers: dict[str, Any] = {}
    for cert in certificates:
        drivers[cert.driver] = {
            "cli_version": cert.cli_version,
            "files": cert.files,
            "lockdown_sha256": cert.lockdown,
            "models": None
            if cert.models is None
            else [{"id": m.id, "cli_model": m.cli_model} for m in cert.models],
        }
    data = {"schema_version": SCHEMA_VERSION, "modelmux_version": __version__, "drivers": drivers}
    return json.dumps(data, indent=2) + "\n"


def write_manifest(path: Path, certificates: list[Certificate]) -> None:
    """Write atomically, readable by everyone, writable by nobody but the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(manifest_json(certificates))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- verify (startup)


def load_certificate(path: Path, driver_name: str) -> Certificate:
    """The manifest's entry for ``driver_name``. Raises ``ManifestError``."""
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestError(f"the manifest {path} is too large")
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise ManifestError(f"no manifest at {path}: this installation is not certified") from None
    except (OSError, ValueError, RecursionError):
        raise ManifestError(f"the manifest {path} is unreadable") from None
    try:
        return _parse_entry(data, driver_name)
    except (KeyError, TypeError, ValueError):
        raise ManifestError(f"the manifest {path} is malformed") from None


def _parse_entry(data: Any, driver_name: str) -> Certificate:
    if data["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(f"unsupported manifest schema {data['schema_version']!r}")
    entry = data["drivers"].get(driver_name)
    if entry is None:
        raise ManifestError(f"the manifest does not certify driver {driver_name!r}")
    files = entry["files"]
    if (
        not isinstance(files, dict)
        or not files
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        raise ValueError("files")
    models: tuple[ModelInfo, ...] | None = None
    if entry["models"] is not None:
        models = tuple(
            ModelInfo(id=validate_model_id(m["id"]), cli_model=validate_cli_model(m["cli_model"]))
            for m in entry["models"]
        )
    version, lockdown = entry["cli_version"], entry["lockdown_sha256"]
    if not isinstance(version, str) or not isinstance(lockdown, str):
        raise ValueError("strings")
    return Certificate(driver_name, version, dict(files), lockdown, models)


async def verify_certificate(cert: Certificate, driver: Driver) -> None:
    """The certificate must belong to this binary and this driver code."""
    if cert.lockdown != lockdown_sha256(driver):
        raise ManifestError(
            f"the {driver.name} lockdown settings changed since certification; "
            "certify again (python -m modelmux certify)"
        )
    try:
        actual = await asyncio.to_thread(file_hashes, driver)
    except OSError:
        raise ManifestError(f"cannot read the {driver.name} CLI files to verify them") from None
    if actual != cert.files:
        raise ManifestError(
            f"the {driver.name} CLI is not the certified one "
            f"(certified version {cert.cli_version}); certify this installation first"
        )


# ---------------------------------------------------------------- command line


def certify_main(argv: list[str]) -> int:
    """``python -m modelmux certify [--output PATH] [--driver NAME=PATH|NAME ...]``.

    Without ``--driver`` every built-in driver is certified, with its CLI from
    ``PATH``. Run inside the image build; no login or API call is needed.
    """
    parser = argparse.ArgumentParser(prog="python -m modelmux certify")
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--driver", action="append", metavar="NAME[=CLI_PATH]",
        help="driver to certify (default: all built-in), optionally with its CLI path",
    )  # fmt: skip
    args = parser.parse_args(argv)
    specs = args.driver or sorted(BUILTIN_DRIVERS)
    certificates = []
    try:
        for spec in specs:
            name, _, cli_path = spec.partition("=")
            cls = load_driver_class(name)
            binary = resolve_binary(cls.binary_name, Path(cli_path) if cli_path else None)
            cert = asyncio.run(certify_driver(cls(binary)))
            models = "at startup" if cert.models is None else f"{len(cert.models)} models"
            print(f"certified {name} {cert.cli_version} ({models})")
            certificates.append(cert)
    except (CertificationError, DriverLoadError, BinaryResolutionError) as exc:
        print(f"modelmux certify: {exc}", file=sys.stderr)
        return 1
    write_manifest(args.output, certificates)
    print(f"wrote {args.output}")
    return 0
