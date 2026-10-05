# ModelMux server

The FastAPI service that exposes an AI coding CLI as an OpenAI-compatible
Chat Completions API. It is shipped as a Docker image (`docker/Dockerfile`).

See the [project README](../README.md) and [docs](../docs/).

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```
