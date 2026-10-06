<div align="center">

<pre>
███╗   ███╗ ██████╗ ██████╗ ███████╗██╗     ███╗   ███╗██╗   ██╗██╗  ██╗ 
████╗ ████║██╔═══██╗██╔══██╗██╔════╝██║     ████╗ ████║██║   ██║╚██╗██╔╝ 
██╔████╔██║██║   ██║██║  ██║█████╗  ██║     ██╔████╔██║██║   ██║ ╚███╔╝  
██║╚██╔╝██║██║   ██║██║  ██║██╔══╝  ██║     ██║╚██╔╝██║██║   ██║ ██╔██╗  
██║ ╚═╝ ██║╚██████╔╝██████╔╝███████╗███████╗██║ ╚═╝ ██║╚██████╔╝██╔╝ ██╗ 
╚═╝     ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝ 
</pre>

**AI coding CLIs as a secure, OpenAI-compatible Chat Completions API.**

Claude Code and Codex, used purely as text-in / text-out models.<br>
No tools, no shell, no filesystem: the CLI only ever thinks, it never acts.

[CLI](docs/CLI.md) · [Setup guide](docs/SETUP.md) · [Commands](docs/COMMANDS.md) · [Security](SECURITY.md) · [Architecture](docs/ARCHITECTURE.md) · [Configuration](docs/CONFIGURATION.md) · [Errors](docs/ERRORS.md) · [Write a driver](docs/WRITING_A_DRIVER.md)

</div>

---

## What it is

ModelMux is a small, self-hosted HTTP service that runs an AI coding CLI
(**Claude Code** via `claude -p`, or **Codex** via `codex exec`) behind an
**OpenAI-compatible `/v1/chat/completions` API**. Your code talks to it with the
OpenAI SDK, LangChain, LangGraph or LiteLLM as if it were any other model.

The CLI is used only as a model. It runs with every tool switched off, in an
empty private workspace, with a minimal environment, inside a read-only,
non-root container. If it ever *tries* to use a tool anyway, a tripwire kills
the whole process group and the request fails with `sandbox_violation`.

Tool calling still works for your application: tools are **simulated**. The
model describes the call it wants, ModelMux validates it and returns it as a
normal OpenAI `tool_calls` response, and **your client** runs the tool.

ModelMux is meant to sit behind a gateway such as LiteLLM, which handles
keys, budgets, routing and fallbacks. ModelMux stays focused: translate, run
safely, report accurately.

## Features

| | |
|---|---|
| **OpenAI compatible** | Chat completions, streaming (SSE, `include_usage`), `tools` / `tool_choice` / `parallel_tool_calls`, `response_format` (`json_object`, `json_schema`, `strict`), `stop`, `/v1/models`, OpenAI-shaped errors |
| **Two drivers** | Claude Code 2.1.x and Codex 0.159+, one image; add more as plugins without touching core code |
| **Locked down** | Every built-in tool, MCP server, hook, plugin, setting file and slash command disabled; each lockdown flag verified at startup |
| **Fail-closed tripwire** | Any tool, command, file, MCP, web-search or unknown execution-like event kills the process group within a second |
| **Robust runtime** | Timeouts (first output, idle, total), output caps, group kill with SIGTERM→SIGKILL, bounded queue, client-disconnect handling |
| **Hardened image** | Non-root (UID 10001), read-only root filesystem, no setuid binaries, `tini`, pinned and checksum-verified CLIs, SBOM + vulnerability scan in CI |
| **Private by default** | No prompts, completions, keys, argv or env in logs; secrets redacted everywhere |

## Quick start

With the `modelmux` CLI (needs Docker and Python 3.10+):

```bash
pipx install modelmux-cli          # or run it without installing: uvx --from modelmux-cli modelmux
modelmux up                        # set up everything, start logged-in providers
modelmux login claude              # guided login (opens your browser), then a real test
modelmux config litellm            # ready-to-paste config for your tools
```

`modelmux status`, `logs`, `doctor`, `upgrade` and `logout` do what they say;
see **[docs/CLI.md](docs/CLI.md)**. To run the containers yourself with
Docker Compose instead, follow **[docs/SETUP.md](docs/SETUP.md)**; every
command is collected in **[docs/COMMANDS.md](docs/COMMANDS.md)**.

## Using it

**curl**

```bash
curl http://127.0.0.1:8101/v1/chat/completions \
  -H "Authorization: Bearer $MODELMUX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello!"}]}'
```

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8101/v1", api_key=MODELMUX_API_KEY)
reply = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(reply.choices[0].message.content)
```

**LangChain**

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="sonnet",
    base_url="http://127.0.0.1:8101/v1",
    api_key=MODELMUX_API_KEY,
    use_responses_api=False,  # required for Codex models, harmless otherwise
)
llm.bind_tools([my_tool]).invoke("...")
```

> LangChain switches `gpt-6*` models used with tools, and any model name
> containing `codex`, to OpenAI's Responses API. ModelMux implements Chat
> Completions only, so set `use_responses_api=False`.

**LiteLLM** (`config.yaml`)

```yaml
model_list:
  - model_name: claude-sonnet
    litellm_params:
      model: openai/sonnet                    # "openai/" = OpenAI-compatible server
      api_base: http://127.0.0.1:8101/v1
      api_key: os.environ/MODELMUX_API_KEY
  - model_name: codex-sol
    litellm_params:
      model: openai/gpt-6.1-sol
      api_base: http://127.0.0.1:8102/v1
      api_key: os.environ/MODELMUX_API_KEY
```

### Models

| Driver | Default model IDs |
|---|---|
| `claude` | `sonnet`, `opus`, `haiku`, `fable`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-1`, `claude-haiku-4-5-20251001` |
| `codex` | `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra` |

Only these IDs are accepted. Restrict or rename them with
`MODELMUX_MODELS='{"fast": "haiku"}'` (public ID → CLI model). Anyone who can
call the API can pick any listed model, so trim the list if cost matters.

### Behaviour worth knowing

- **Streaming**: Claude streams token deltas. Codex only reports the finished
  message, so it arrives as one chunk. Headers are sent once the first text
  is ready, so errors before that are normal HTTP errors; errors after that
  arrive as one `error` event followed by `[DONE]`.
- **Tools and structured output** are validated before anything is sent, so
  those requests are buffered even with `stream: true`. Invalid model output
  gets exactly one corrective retry, then `502 invalid_model_output`.
- **Ignored parameters** (`temperature`, `max_tokens`, `seed`, ...) are
  accepted and listed in the `X-ModelMux-Ignored-Params` response header: the
  CLIs cannot honour them.
- **One worker per container.** Concurrency limits are per process; scale out
  by running more containers.

## Security in one paragraph

Defence in depth: CLI flags disable every tool; each flag is verified at
startup; a per-request random boundary stops role spoofing; only allowlisted
model values reach argv and never a value starting with `-`; the environment
is built from scratch; the workspace is empty and private; any tool event
kills the process group; the container is read-only, non-root and has no
capabilities; logs never contain prompts or keys. Restrict the containers'
egress to the provider's domains. Details and the threat model:
**[SECURITY.md](SECURITY.md)**.

## Limitations

- Text only: image, audio and file inputs are rejected.
- Chat Completions only: no Responses API, embeddings or Anthropic Messages API.
- `n` must be 1; `logprobs` are not supported.
- Provider login is not managed by ModelMux: log each CLI in inside its volume.
- The Codex driver was verified offline against codex-cli 0.159.2 (flags,
  features, config keys, auth failure). A successful Codex response has not
  been observed live; its event shapes follow the Codex source.

## Repository layout

```
modelmux/
├── server/        # the ModelMux server (Python 3.12, FastAPI), shipped as a Docker image
├── cli/
│   └── python/    # the modelmux CLI (modelmux-cli on PyPI, Python 3.10+, no dependencies)
├── shared/        # language-neutral data the CLIs share: providers, templates, release pin
├── docker/        # Dockerfile, compose example, pinned AI CLI lockfile
├── docs/          # CLI, setup, commands, architecture, configuration, errors, drivers, releasing
└── .github/       # CI (server, CLI on Linux/macOS/Windows, image scan) and the release workflow
```

Components share **data** (`shared/`), never code, so a CLI in another
language (for example a Node launcher) can be added next to `cli/python/`.

## Development

```bash
# Server
cd server
uv sync                                     # Python 3.12, locked dependencies
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                               # ~800 tests, fake CLIs, no network
LIVE_DRIVER_HOME=$HOME uv run pytest -m live tests/live   # real Claude (costs a few requests)

# CLI
cd cli/python
uv sync                                     # Python 3.10+, no runtime dependencies
uv run ruff check . && uv run mypy && uv run pytest
```

Releases: see **[docs/RELEASING.md](docs/RELEASING.md)**.

## License

Apache-2.0. See [LICENSE](LICENSE).
