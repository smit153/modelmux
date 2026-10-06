# Commands and prerequisites

Every command needed to build, run, test and operate ModelMux, in order.
Explanations are in [SETUP.md](SETUP.md). Run everything from the repository
root unless a step says otherwise.

## With the `modelmux` CLI

```bash
pipx install modelmux-cli                 # or: uvx --from modelmux-cli modelmux <command>
modelmux up [claude|codex] [--image REF] [--port claude=9101]
modelmux login claude [--method console] [--no-browser] [--raw] [--force]
modelmux login codex  [--method api-key]
modelmux status
modelmux logs claude -f
modelmux config litellm|openai-python|langchain|curl|env [--provider claude] [--reveal-key]
modelmux key show
modelmux doctor
modelmux upgrade
modelmux logout codex [--yes]
modelmux down
```

Details: [CLI.md](CLI.md). The rest of this page does everything by hand with
Docker Compose.

```bash
C="docker compose -f docker/compose.example.yaml"   # used throughout this page
```

## Prerequisites checklist

```bash
docker --version              # Docker Engine 24+
docker compose version        # Compose v2
git --version
df -h .                       # ~3 GB free
curl --version                # for the checks below

# Only for development and tests:
python3.12 --version
uv --version                  # https://docs.astral.sh/uv/
```

| Tool | Required for | Install |
|---|---|---|
| Docker Engine + Compose v2 | running ModelMux | https://docs.docker.com/engine/install/ |
| git | getting the code | your package manager |
| curl | verifying | your package manager |
| Python 3.12 | development only | your package manager / pyenv |
| uv | development only | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node 22+ / npm | only to regenerate `docker/cli/package-lock.json` | https://nodejs.org |

Accounts: a Claude account for the `claude` driver and/or an OpenAI/ChatGPT
account with Codex access for the `codex` driver.

## 1. Get the code

```bash
git clone https://github.com/smit153/modelmux.git
cd modelmux
```

## 2. Configure

```bash
cp docker/.env.example docker/.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # new client key
# edit docker/.env:  MODELMUX_API_KEYS=<key>[,<key2>...]
```

## 3. Build

```bash
$C build                                                      # both services, one image
docker image ls modelmux                                      # modelmux:latest

# Different CLI versions (after updating docker/cli/package*.json):
$C build --build-arg CLAUDE_CODE_VERSION=2.1.285 --build-arg CODEX_VERSION=0.159.2

# Plain docker instead of compose:
docker build -f docker/Dockerfile -t modelmux:latest .
```

## 4. Log in (once per driver)

```bash
# Claude
$C run --rm -e HOME=/home/modelmux/driver-home --entrypoint claude modelmux-claude auth login
$C run --rm -e HOME=/home/modelmux/driver-home --entrypoint claude modelmux-claude auth login --console   # API billing
$C run --rm -e HOME=/home/modelmux/driver-home --entrypoint claude modelmux-claude auth status

# Codex
$C run --rm -e HOME=/home/modelmux/driver-home --entrypoint codex modelmux-codex login --device-auth
printenv OPENAI_API_KEY | $C run --rm -T -e HOME=/home/modelmux/driver-home --entrypoint codex modelmux-codex login --with-api-key
$C run --rm -e HOME=/home/modelmux/driver-home --entrypoint codex modelmux-codex login status
```

## 5. Run

```bash
$C up -d modelmux-claude                 # http://127.0.0.1:8101
$C up -d modelmux-codex                  # http://127.0.0.1:8102
$C up -d                                 # both
$C ps                                    # state and health
$C logs -f modelmux-claude               # JSON logs
$C restart modelmux-claude
$C down                                  # stop, keep logins
$C down -v                               # stop and delete the login volumes
```

## 6. Verify

```bash
export MODELMUX_API_KEY=<key from docker/.env>
BASE=http://127.0.0.1:8101               # 8102 for Codex

curl -s $BASE/health/live
curl -s $BASE/health/ready
curl -s $BASE/v1/models -H "Authorization: Bearer $MODELMUX_API_KEY"

# Chat
curl -s $BASE/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"Say hello"}]}'

# Streaming with usage
curl -sN $BASE/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonnet","stream":true,"stream_options":{"include_usage":true},
       "messages":[{"role":"user","content":"Count to 5"}]}'

# Tool call (the model returns tool_calls; your client runs the tool)
curl -s $BASE/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"Weather in Oslo?"}],
       "tools":[{"type":"function","function":{"name":"get_weather",
         "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'

# Structured output
curl -s $BASE/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"Ada Lovelace: name and age at death"}],
       "response_format":{"type":"json_schema","json_schema":{"name":"person","strict":true,
         "schema":{"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"}},
                   "required":["name","age"]}}}}'

# Metrics (only with MODELMUX_ENABLE_METRICS=true)
curl -s $BASE/metrics -H "Authorization: Bearer $MODELMUX_API_KEY"
```

## 7. Inspect the hardening

```bash
$C exec modelmux-claude id                                  # uid=10001(modelmux)
$C exec modelmux-claude sh -c 'touch /usr/x'                # Read-only file system
$C exec modelmux-claude cat /proc/1/comm                    # tini
docker inspect modelmux-modelmux-claude-1 \
  --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}}'
```

## 8. Security scan and SBOM

```bash
docker save modelmux:latest -o /tmp/modelmux.tar

# Fails on fixable HIGH/CRITICAL (same policy as CI)
docker run --rm -v /tmp:/work -v "$PWD/.trivyignore:/work/.trivyignore:ro" aquasec/trivy \
  image --input /work/modelmux.tar --severity HIGH,CRITICAL --ignore-unfixed \
  --ignorefile /work/.trivyignore --scanners vuln --exit-code 1

# CycloneDX SBOM
docker run --rm -v /tmp:/work aquasec/trivy \
  image --input /work/modelmux.tar --format cyclonedx --output /work/sbom.cdx.json
```

## 9. Upgrade a CLI

```bash
# 1. edit the version in docker/cli/package.json
cd docker/cli && npm install --package-lock-only --no-audit --no-fund && cd ../..
# 2. rebuild with the matching build argument (or update the ARG default)
$C build --build-arg CLAUDE_CODE_VERSION=<new>
# 3. restart; the startup probe re-verifies every lockdown flag
$C up -d modelmux-claude && $C logs modelmux-claude | grep -E "probe_ok|probe_failed"
```

## 10. Development (no Docker needed)

```bash
cd server                                        # the server's Python project
uv sync                                          # create .venv from uv.lock
uv run ruff check . && uv run ruff format --check .
uv run mypy                                      # strict
uv run pytest                                    # all offline tests (fake CLIs)
uv run pytest --cov --cov-report=term-missing
uv run pytest tests/security                     # security suite only
uv run pytest tests/contract                     # OpenAI SDK + LangChain

# Live tests against your real Claude login (a few small sonnet requests)
LIVE_DRIVER_HOME=$HOME uv run pytest -m live tests/live

# Run the server locally with your own Claude login
MODELMUX_DRIVER=claude MODELMUX_API_KEYS=<32+ chars> \
MODELMUX_DRIVER_HOME=$HOME MODELMUX_WORK_ROOT=/tmp/modelmux \
uv run python -m modelmux
```

## 11. Clean up

```bash
$C down -v                                # containers, network, login volumes
docker rmi modelmux:latest                # the image
docker builder prune                      # build cache (optional)
```
