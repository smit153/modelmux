# Configuration

All settings come from environment variables prefixed with `MODELMUX_`. They
are validated at startup; an invalid value stops the process with a message
that names the setting but never prints its value. In Docker, put them in
`docker/.env` (see [SETUP.md](SETUP.md)).

> `tests/unit/test_docs.py` fails if a setting is missing from this page.

## Driver

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_DRIVER` | **required** | Driver to load: `claude`, `codex`, or an installed plugin name. Only this driver is imported. |
| `MODELMUX_CLI_PATH` | found on `PATH` | Absolute path to the CLI binary. It must exist, be executable, and not be world-writable (nor in a world-writable directory without the sticky bit). |
| `MODELMUX_DRIVER_HOME` | `/home/modelmux/driver-home` | Absolute path used as the CLI's `HOME`. Holds its login state (`.claude/`, `.codex/`). One per driver; never share it. |
| `MODELMUX_WORK_ROOT` | `/tmp/modelmux` | Absolute path for per-request workspaces (mode `0700`, random names, removed after each request; leftovers are removed at startup). |
| `MODELMUX_MODELS` | driver default | JSON object mapping public model IDs to CLI model values, e.g. `{"fast": "haiku", "smart": "opus"}`. Replaces the default list. IDs must match `^[a-zA-Z0-9._:-]{1,64}$`; values must match `^[a-zA-Z0-9._:\-\[\]]{1,64}$` and must not start with `-`. |

## Inbound authentication

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_API_KEYS` | **required** | Comma-separated bearer keys clients must send (`Authorization: Bearer <key>`). Each at least 32 characters, no whitespace. |
| `MODELMUX_ALLOW_NO_AUTH` | `false` | Development only: allow running without keys. Logs a warning at startup and every 1000th request. Ignored when keys are set. |

## HTTP

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_HOST` | `0.0.0.0` | Bind address inside the container. Publish the port on `127.0.0.1` only. |
| `MODELMUX_PORT` | `8000` | Listen port. |
| `MODELMUX_CORS_ORIGINS` | *(empty: no CORS)* | Comma-separated origins allowed by CORS. |
| `MODELMUX_TRUSTED_PROXIES` | *(empty)* | Comma-separated proxy IPs whose `X-Forwarded-*` headers are trusted. Empty means proxy headers are ignored. |
| `MODELMUX_ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json`. |
| `MODELMUX_ENABLE_METRICS` | `false` | Serve Prometheus metrics at `/metrics` (requires a bearer key). |

## Concurrency

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_MAX_CONCURRENT_PROCESSES` | `2` | CLI processes running at once (per container; ModelMux always runs one worker). |
| `MODELMUX_MAX_QUEUE_SIZE` | `16` | Requests allowed to wait for a slot. When full: `503 overloaded` with `Retry-After: 5`. |
| `MODELMUX_QUEUE_TIMEOUT` | `30` | Seconds a request may wait for a slot before `503 overloaded`. |

## Timeouts (seconds)

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_FIRST_OUTPUT_TIMEOUT` | `60` | Maximum wait for the first complete output line after starting the CLI. |
| `MODELMUX_IDLE_TIMEOUT` | `120` | Maximum wait between output lines (counted only while waiting, not while a slow client reads). |
| `MODELMUX_TOTAL_TIMEOUT` | `600` | Wall-clock budget per request, shared with the repair attempt. |
| `MODELMUX_KILL_GRACE` | `3` | Time between SIGTERM and SIGKILL when stopping a CLI's process group. |

Both `FIRST_OUTPUT_TIMEOUT` and `IDLE_TIMEOUT` must not exceed `TOTAL_TIMEOUT`.
Any timeout returns `504 provider_timeout`.

## Output caps (bytes)

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_MAX_LINE_BYTES` | `4194304` (4 MiB) | Longest single output line. Must not exceed `MAX_STDOUT_BYTES`. |
| `MODELMUX_MAX_STDOUT_BYTES` | `33554432` (32 MiB) | Total CLI output per run. Exceeding either cap kills the run: `502 output_too_large`. |
| `MODELMUX_MAX_STDERR_BYTES` | `16384` (16 KiB) | Size of the stderr ring buffer kept for logs (never returned to clients). |

## Input limits

All checked before any process is started.

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_MAX_BODY_BYTES` | `2097152` (2 MiB) | Request body size, enforced while reading (including chunked bodies). `413 payload_too_large`. |
| `MODELMUX_MAX_MESSAGES` | `500` | Messages per request. |
| `MODELMUX_MAX_TOOLS` | `64` | Tools per request. |
| `MODELMUX_MAX_TOOL_SCHEMA_BYTES` | `65536` (64 KiB) | Size of one tool definition. |
| `MODELMUX_MAX_TOOL_ARGUMENTS_BYTES` | `262144` (256 KiB) | Size of the arguments of one simulated tool call returned by the model. |
| `MODELMUX_MAX_PROMPT_BYTES` | `1572864` (1.5 MiB) | Rendered prompt (system + transcript). `413 context_too_large`. |
| `MODELMUX_MAX_STOP_SEQUENCES` | `4` | Stop sequences per request. |

## Logging

| Variable | Default | Description |
|---|---|---|
| `MODELMUX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `MODELMUX_LOG_CONTENT` | `false` | **Debug only.** Also log prompts and completions (at `DEBUG`, still redacted). Logs a warning at startup. |

Logs are JSON, one object per line on stdout, with `request_id`, `driver` and
`model` where known. By default they contain sizes, counts, durations, exit
codes and error codes, never content, keys, headers, argv or environment.

## Container environment

Set by the image and normally left alone: `MODELMUX_HOST=0.0.0.0`,
`MODELMUX_PORT=8000`, `MODELMUX_DRIVER_HOME=/home/modelmux/driver-home`,
`MODELMUX_WORK_ROOT=/tmp/modelmux`, `DISABLE_AUTOUPDATER=1`.

## Environment passed to the CLI

The CLI never inherits ModelMux's environment. It gets exactly:
`PATH=/usr/local/bin:/usr/bin:/bin`, `HOME=<MODELMUX_DRIVER_HOME>`, `LANG`,
`LC_ALL`, `TZ` (from ModelMux's environment, default `C.UTF-8` / `UTC`), plus
the driver's allowlist:

| Driver | Extra variables |
|---|---|
| `claude` | `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| `codex` | none |

Provider keys such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` are **not**
forwarded: log the CLI in inside its driver-home volume instead.
