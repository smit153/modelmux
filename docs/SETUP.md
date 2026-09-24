# Setup guide

This guide takes you from a fresh machine to a running, locked-down ModelMux
container that answers OpenAI-style requests. Every command is also listed,
without the explanations, in [COMMANDS.md](COMMANDS.md).

```
  1. Prerequisites      →  Docker, git (and uv only for development)
  2. Get the code       →  git clone
  3. Configure          →  docker/.env with your API key(s)
  4. Build the image    →  one image for both drivers
  5. Log the CLI in     →  once, inside its own Docker volume
  6. Start              →  docker compose up
  7. Verify             →  health check + first request
  8. Put it in front    →  LiteLLM / your app, restrict egress
```

---

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| **Docker Engine 24+** with **Compose v2** | builds and runs the image | `docker --version && docker compose version` |
| **git** | gets the code | `git --version` |
| ~**3 GB free disk** | the image is ~1.35 GB (the CLIs' native binaries) plus build cache | `df -h .` |
| Outbound **HTTPS** from the host | the CLIs call their provider; the build downloads packages | — |
| A **Claude** subscription/Console account (for `claude`) and/or an **OpenAI/ChatGPT** account with Codex access (for `codex`) | the CLIs need to be logged in | — |
| **Python 3.12 + [uv](https://docs.astral.sh/uv/)** | *only* to develop or run the tests | `uv --version` |

Linux x86-64 and arm64 hosts are supported by the image (the CLIs ship native
binaries for both). On macOS and Windows use Docker Desktop.

## 2. Get the code

```bash
git clone https://github.com/smit153/modelmux.git
cd modelmux
```

All commands below are run from the repository root.

## 3. Configure

ModelMux itself needs only one secret: the key(s) **your clients** use to call
it. It is not a provider key.

```bash
cp docker/.env.example docker/.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # prints a new key
```

Put the key into `docker/.env`:

```bash
MODELMUX_API_KEYS=<paste the key here>
```

- Several keys: separate them with commas. Each must be at least 32 characters.
- `docker/.env` is git-ignored. Never commit it.
- Every other setting has a safe default. See [CONFIGURATION.md](CONFIGURATION.md)
  if you want to change limits, timeouts or the model list.

## 4. Build the image

```bash
docker compose -f docker/compose.example.yaml build
```

This builds `modelmux:latest`, which contains ModelMux, **Claude Code 2.1.285**
and **Codex 0.159.2**. The CLI versions are pinned in
`docker/cli/package-lock.json`, and `npm ci` verifies every package's checksum.
The build fails if the installed versions do not match the build arguments.

## 5. Log the CLI in (once)

Each driver keeps its login state in its **own Docker volume**
(`modelmux_claude-home`, `modelmux_codex-home`). ModelMux never creates or
manages credentials; you log each CLI in once, inside its volume, with a
one-off container that has the same hardening as the service.

### Claude

```bash
docker compose -f docker/compose.example.yaml run --rm \
  -e HOME=/home/modelmux/driver-home \
  --entrypoint claude modelmux-claude auth login
```

The CLI prints a URL. Open it in your browser, sign in, and paste the code
back into the terminal. Use `auth login --console` instead to bill an
Anthropic Console (API) account.

Check it worked:

```bash
docker compose -f docker/compose.example.yaml run --rm \
  -e HOME=/home/modelmux/driver-home \
  --entrypoint claude modelmux-claude auth status
```

`"loggedIn": true` means you are done.

### Codex

Device login (ChatGPT account):

```bash
docker compose -f docker/compose.example.yaml run --rm \
  -e HOME=/home/modelmux/driver-home \
  --entrypoint codex modelmux-codex login --device-auth
```

Or with an OpenAI API key (read from stdin, so it never appears in the
process list or shell history):

```bash
printenv OPENAI_API_KEY | docker compose -f docker/compose.example.yaml run --rm -T \
  -e HOME=/home/modelmux/driver-home \
  --entrypoint codex modelmux-codex login --with-api-key
```

Check it worked:

```bash
docker compose -f docker/compose.example.yaml run --rm \
  -e HOME=/home/modelmux/driver-home \
  --entrypoint codex modelmux-codex login status
```

> **Why `HOME=/home/modelmux/driver-home`?** When ModelMux runs the CLI, it
> sets `HOME` to the driver-home volume, so the CLI must find its login there.
> Claude stores it in `~/.claude`, Codex in `~/.codex`.

> **Verified vs. not:** the `auth status` / `login status` commands above were
> tested in the hardened container. The interactive login flows need your
> browser and account and were not run as part of this project.

## 6. Start

Start the driver(s) you logged in:

```bash
docker compose -f docker/compose.example.yaml up -d modelmux-claude
docker compose -f docker/compose.example.yaml up -d modelmux-codex     # optional
```

| Service | Driver | Address (host only) |
|---|---|---|
| `modelmux-claude` | `claude` | `http://127.0.0.1:8101` |
| `modelmux-codex` | `codex` | `http://127.0.0.1:8102` |

At startup ModelMux checks the CLI before it serves anything:

1. the CLI version is supported,
2. **every lockdown flag** (and, for Codex, every feature name and config key)
   is accepted, offline, with no API call,
3. one tiny real request (`"Reply with exactly the word: ok"`) succeeds.

If any step fails the container **exits** with a clear message instead of
serving in a half-working state. Read it with:

```bash
docker compose -f docker/compose.example.yaml logs modelmux-claude
```

## 7. Verify

```bash
export MODELMUX_API_KEY=<the key from docker/.env>

curl -s http://127.0.0.1:8101/health/live        # {"status":"ok"}
curl -s http://127.0.0.1:8101/health/ready       # {"status":"ready"}

curl -s http://127.0.0.1:8101/v1/models -H "Authorization: Bearer $MODELMUX_API_KEY"

curl -s http://127.0.0.1:8101/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Say hello"}]}'
```

Streaming:

```bash
curl -sN http://127.0.0.1:8101/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "stream": true, "messages": [{"role": "user", "content": "Count to 5"}]}'
```

For Codex use port `8102` and model `gpt-6.1-sol`.

## 8. Put it in front of your apps

- **Gateway**: point LiteLLM (or your app) at `http://127.0.0.1:8101/v1` with
  the ModelMux key. See the LiteLLM example in the [README](../README.md#using-it).
  If LiteLLM also runs in Docker, attach it to the `modelmux_modelmux` network
  and use `http://modelmux-claude:8000/v1`.
- **LangChain**: set `use_responses_api=False` on `ChatOpenAI`.
- **Behind a reverse proxy**: set `MODELMUX_TRUSTED_PROXIES` to the proxy's IPs
  so `X-Forwarded-*` headers are honoured; otherwise they are ignored.
- **Restrict egress** (strongly recommended). The containers only need HTTPS to
  their provider:
  - Claude: `api.anthropic.com` (plus the login endpoints while logging in)
  - Codex: `api.openai.com` / `chatgpt.com` (plus the login endpoints)

  Enforce this with a host firewall or an egress proxy. Docker's `internal`
  networks block all egress, so they cannot be used for this.
- **Never** mount the Docker socket or host directories into these containers,
  and never share one driver-home volume between services.

## Day-2 operations

**Logs** (structured JSON, one object per line, no prompts or keys):

```bash
docker compose -f docker/compose.example.yaml logs -f modelmux-claude
```

**Upgrading a CLI**:

1. Change the version in `docker/cli/package.json`.
2. Regenerate the lockfile: `cd docker/cli && npm install --package-lock-only`.
3. Pass the new version to the build:
   `docker compose -f docker/compose.example.yaml build --build-arg CLAUDE_CODE_VERSION=<new>`
   (or update the `ARG` defaults in `docker/Dockerfile`).
4. Start it. The startup probe refuses to run if a lockdown flag disappeared
   in the new version, so a breaking CLI change cannot silently weaken the
   sandbox.

**Stopping and removing**:

```bash
docker compose -f docker/compose.example.yaml down        # keeps logins
docker compose -f docker/compose.example.yaml down -v     # also deletes the login volumes
```

## Troubleshooting

| Message | Meaning | Fix |
|---|---|---|
| `invalid configuration: MODELMUX_API_KEYS: ...` | missing or short key | set a ≥ 32-character key in `docker/.env` |
| `probe failed: live check failed: auth` | the CLI is not logged in (or the login expired) | step 5 |
| `probe failed: CLI does not support lockdown flag --x` | the CLI version changed a flag | pin the previous CLI version; report it |
| `probe failed: CLI version X is not in ...` | unsupported CLI version | use the pinned version |
| `probe failed: live check failed: rate_limited` | provider quota reached | wait, or check your plan |
| HTTP `503 overloaded` with `Retry-After` | all CLI slots busy and the queue is full | retry, or raise `MODELMUX_MAX_CONCURRENT_PROCESSES` / run more containers |
| HTTP `502 sandbox_violation` | the model tried to use a tool; the process was killed | expected behaviour; nothing ran |
| HTTP `404` on `/v1/responses` from LangChain | LangChain switched to the Responses API | `ChatOpenAI(use_responses_api=False)` |

All error codes are listed in [ERRORS.md](ERRORS.md).
