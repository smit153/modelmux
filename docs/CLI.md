# The `modelmux` CLI

`modelmux` sets up, logs in to and runs ModelMux on your machine. It drives
Docker for you, guides you through each provider's login, and prints
ready-to-paste config for your tools.

```bash
pipx install modelmux-cli        # or run it without installing: uvx --from modelmux-cli modelmux
npm install -g modelmux-cli      # or with Node.js instead (0.3.0+): npx modelmux-cli
modelmux up                      # prepare everything, start logged-in providers
modelmux login claude            # guided login, then a real test
modelmux config litellm          # paste into your LiteLLM config
```

Requirements: **Docker with Compose v2** (Docker Desktop on macOS and
Windows) and **Python 3.10+** or **Node.js 22+**. The CLI has no other
dependencies.

The Python package (PyPI) and the Node package (npm) are the same CLI: the
same commands, options, messages and exit codes, the same settings directory
and the same server image. Use whichever runtime you have; both can manage
the same setup.

## Commands

| Command | What it does |
|---|---|
| `modelmux up [provider…] [--image REF] [--port P=PORT]` | Checks Docker, creates the API key and settings, downloads the server image, creates login volumes, and starts every provider that is logged in. Safe to run again: providers that are already running and unchanged are left alone. |
| `modelmux login <provider> [--method M] [--no-browser] [--raw] [--force]` | Runs the provider's own login in a short-lived helper container, opens the link in your browser, then restarts the provider and waits until it answers: **✓ logged in and tested**. Already logged in: just makes sure it runs. |
| `modelmux logout <provider> [--yes]` | Stops the provider, lets it revoke its login, and deletes **only** that provider's login volume. Asks first unless `--yes`. |
| `modelmux status` | One line per provider: container, login, health, URL. |
| `modelmux logs [provider] [-f] [--tail N]` | The server logs (JSON lines; never prompts or keys). |
| `modelmux config <litellm\|openai-python\|langchain\|curl\|env> [--provider P] [--reveal-key]` | Ready-to-paste client config. The key is referenced as `$MODELMUX_API_KEY` unless you add `--reveal-key`. Only the snippet goes to stdout. |
| `modelmux key show` | Prints the API key your clients use. |
| `modelmux doctor` | Checks Docker, network, versions, file permissions, image, and each provider's login, container, health and API, with a fix for every problem. |
| `modelmux upgrade [--image REF]` | Runs the server image that matches this CLI version and recreates running providers. Logins are kept. Tells you when a newer CLI exists. |
| `modelmux down` | Stops everything. Logins are kept. |

Global options: `-v/--verbose` (show Docker commands and their output,
secrets masked), `--no-color`, `--version`.

## Providers

| Provider | Login methods | Default port |
|---|---|---|
| `claude` (Claude Code) | `browser` (subscription, default), `console` (Anthropic Console / API billing) | 8101 |
| `codex` (OpenAI Codex) | `device` (ChatGPT account, default), `api-key` (asked for without echo) | 8102 |

Provider details live in `shared/providers/<name>.json`; see
[shared/README.md](../shared/README.md).

## How login works

1. The provider's own login command (for example `claude auth login`) runs in
   a **helper container** from the same image, with the same hardening as the
   server (read-only, no capabilities, non-root) and only that provider's
   login volume mounted.
2. On **Linux and macOS** the CLI spots the login link in the provider's
   output, opens your browser and still lets you type or paste the code. On
   **Windows**, or with `--raw`, your console is attached directly: open the
   link the provider prints.
   - The Python CLI runs the helper on its own pseudo-terminal.
   - The Node CLI has no pseudo-terminal of its own (that would need a
     native add-on). Docker gives the helper a terminal fed straight from
     yours, and only the output passes through the CLI to find the link. So
     the Node CLI needs a real terminal to log in, and the provider sees no
     window size (both providers print the link unwrapped either way).
3. Afterwards the CLI checks the login, recreates the provider's container and
   waits for `/health/ready`. The server's startup check makes one tiny real
   request, so ready means the login works.

Your keystrokes, pasted codes and API keys go straight to the provider CLI;
the ModelMux CLI never stores or logs them. Ctrl+C, a closed terminal or a
timeout removes the helper container.

## Files

| OS | Settings directory |
|---|---|
| Linux | `$XDG_CONFIG_HOME/modelmux` (usually `~/.config/modelmux`) |
| macOS | `~/Library/Application Support/modelmux` |
| Windows | `%APPDATA%\modelmux` |

Override it with `MODELMUX_CLI_HOME`. It contains:

| File | What |
|---|---|
| `config.json` | Ports and the image override, if any |
| `secrets.env` | `MODELMUX_API_KEYS=<key>`, mode `0600` (generated with `secrets`) |
| `compose.yaml` | Generated compose file (JSON); rewritten by `up` |

Logins are **not** stored here: each lives in its own Docker volume
(`modelmux_claude-home`, `modelmux_codex-home`), which only `modelmux logout`
deletes.

## The server image

Each CLI release runs the server image built in the same release, pinned by
digest (`ghcr.io/smit153/modelmux@sha256:…`), never `latest`. To get a newer
server, upgrade the CLI (`pipx upgrade modelmux-cli`, or
`npm install -g modelmux-cli@latest`) and run
`modelmux upgrade`. `--image REF` overrides it (remembered in `config.json`),
for example a locally built `modelmux:dev`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A problem you can fix (the message says how) |
| 2 | Wrong usage (unknown provider, bad option) |
| 3 | Docker problem (missing, stopped, no permission, pull failed) |
| 130 | Cancelled (Ctrl+C, terminal closed) |

## Troubleshooting

Start with `modelmux doctor`. Common messages:

| Message | Fix |
|---|---|
| Docker is installed but not running | Start Docker Desktop, or `sudo systemctl start docker` |
| Your user is not allowed to use Docker | `sudo usermod -aG docker $USER`, then log out and back in |
| Port 8101 … is already in use | `modelmux up --port claude=9101` |
| … stopped while starting / the login has expired | `modelmux login <provider> --force` |
| The CLI inside the image is not supported | `modelmux upgrade` (and upgrade the CLI) |
| Docker could not reach the internet | Check your connection or proxy (`HTTPS_PROXY`, Docker Desktop proxy settings) |
| This development build has no pinned server image | Pass `--image modelmux:dev` (build it from `docker/Dockerfile`) |

## Development

```bash
cd cli/python
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run modelmux --help
```

```bash
cd cli/node                      # TypeScript, compiled to dist/ for npm
npm ci
npm run typecheck                # tsc, strict
npm test                         # node:test, runs the .ts files directly
npm run build && node dist/bin.js --help
```

The Node tests include the Python CLI's exact help and error output
(`cli/node/test/fixtures/argparse`), so the two parsers cannot drift apart.
Only `docker.ts` (and `browser.ts`, for the system's link opener) may start
programs, never through a shell; a test enforces it, like `test_source_scan.py`
does for Python.
