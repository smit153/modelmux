# syntax=docker/dockerfile:1
#
# ModelMux: one image, both drivers (select with MODELMUX_DRIVER).
# Runs as UID 10001 with a read-only root filesystem; the only writable paths
# are /tmp (tmpfs) and the driver-home volume.
#
#   docker build -f docker/Dockerfile -t modelmux .

ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG NODE_IMAGE=node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

# ---------------------------------------------------------------- python deps
FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION=0.12.18
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
# Dependencies first (cached), then the project itself, non-editable.
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------- CLIs
FROM ${NODE_IMAGE} AS clis
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.159.2
WORKDIR /opt/clis
COPY docker/cli/package.json docker/cli/package-lock.json ./
# npm ci installs exactly the lockfile and verifies every package's sha512.
RUN npm ci --omit=dev --no-audit --no-fund \
 && node -e ' \
      const want = {"@anthropic-ai/claude-code": process.argv[1], "@openai/codex": process.argv[2]}; \
      for (const [name, version] of Object.entries(want)) { \
        const got = require(`/opt/clis/node_modules/${name}/package.json`).version; \
        if (got !== version) { console.error(`${name} is ${got}, expected ${version}`); process.exit(1); } \
      }' "${CLAUDE_CODE_VERSION}" "${CODEX_VERSION}" \
 && rm -rf /root/.npm

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.1.0
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.159.2
LABEL org.opencontainers.image.title="modelmux" \
      org.opencontainers.image.description="AI coding CLIs as a locked-down OpenAI-compatible API" \
      org.opencontainers.image.source="https://github.com/modelmux/modelmux" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.modelmux.claude-code.version="${CLAUDE_CODE_VERSION}" \
      io.modelmux.codex.version="${CODEX_VERSION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
 && groupadd --gid 10001 modelmux \
 && useradd --uid 10001 --gid 10001 --home-dir /home/modelmux --create-home \
      --shell /usr/sbin/nologin modelmux \
 && install -d -o modelmux -g modelmux -m 0700 /home/modelmux/driver-home \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} +

COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis /opt/clis/node_modules /opt/clis/node_modules
RUN ln -s /opt/clis/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
 && ln -s /opt/clis/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
 && chmod -R go-w /opt/clis \
 && claude --version && codex --version

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    MODELMUX_HOST=0.0.0.0 \
    MODELMUX_PORT=8000 \
    MODELMUX_DRIVER_HOME=/home/modelmux/driver-home \
    MODELMUX_WORK_ROOT=/tmp/modelmux

USER 10001:10001
WORKDIR /home/modelmux
VOLUME ["/home/modelmux/driver-home"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MODELMUX_PORT', '8000') + '/health/live', timeout=3)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "modelmux"]
