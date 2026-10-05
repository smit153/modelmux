"""ModelMux: AI coding CLIs as an OpenAI-compatible Chat Completions API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("modelmux")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
