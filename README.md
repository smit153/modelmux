# ModelMux

ModelMux is a small, self-hosted HTTP service that exposes AI coding CLIs
(Claude Code via `claude -p`, Codex via `codex exec`) as an
**OpenAI-compatible Chat Completions API**.

Each CLI is used purely as a text-in, text-out model backend. The CLI never
executes tools, runs commands, or touches the filesystem; tool calling is
simulated and executed by the API client.

> Status: under active development. See `docs/` as phases land.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Licensed under Apache-2.0.
