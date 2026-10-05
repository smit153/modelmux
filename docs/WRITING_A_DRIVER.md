# Writing a driver

A driver teaches ModelMux how to use one CLI as a text-only model. Adding a
driver never requires changes to `core/`, `runtime/` or `api/`.

A driver **translates**; it never executes. It builds an argv list, parses
output lines, classifies failures and describes how to probe the CLI. The
runtime spawns, supervises and kills the process; the pipeline enforces the
tripwire.

## The contract

```python
from modelmux.drivers.base import (
    Driver,
    DriverRequest,
    Invocation,
    ModelInfo,
    ProbeContext,
    ProbeResult,
)
from modelmux.drivers.events import NormalizedEvent
from modelmux.errors import ModelMuxError


class MyDriver(Driver):
    name = "mycli"  # the value of MODELMUX_DRIVER
    binary_name = "mycli"  # looked up on PATH unless MODELMUX_CLI_PATH is set
    supported_versions = ">=1.4,<2"  # PEP 440 specifier, checked by probe()

    def models(self) -> list[ModelInfo]: ...
    async def probe(self, ctx: ProbeContext) -> ProbeResult: ...
    def build_invocation(self, req: DriverRequest) -> Invocation: ...
    def parse_line(self, line: bytes) -> list[NormalizedEvent]: ...
    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None: ...
    def env_allowlist(self) -> frozenset[str]: ...  # optional, default: none
```

| Method | Responsibility |
|---|---|
| `models()` | The default allowlist: public model ID → CLI `--model` value. The only models accepted (users can replace it with `MODELMUX_MODELS`). |
| `probe(ctx)` | Runs at startup through `ctx.run(build, budget=...)`. Check the version, check that **every lockdown flag you pass is accepted**, then make one tiny live request. Return `ProbeResult(ok=False, reason=...)` with a short, secret-free reason; the service will not start. |
| `build_invocation(req)` | Return `Invocation(argv, stdin, env, files)`. `argv[0]` must be `self.binary` (use `self.argv(...)`). **Only `req.cli_model` may come from the request**, and it is already allowlisted. Send the transcript on stdin; put the system prompt in a private file (`files={"system-prompt.txt": ...}` is written with mode `0600` into `req.workspace`). |
| `parse_line(line)` | Turn one stdout line into zero or more events. Must **never raise**; return `ProviderFailure(FailureKind.PROTOCOL, ...)` for garbage (including `RecursionError` from deeply nested JSON). |
| `classify_exit(...)` | Map a non-zero exit to a specific error using the stderr tail, or return `None` for the generic `provider_error`. |
| `env_allowlist()` | Names of extra environment variables your invocation sets or passes through. `MODELMUX_*` and the core variables (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`) are refused. |

### Events

| Event | Emit when |
|---|---|
| `TextDelta(text)` | Incremental assistant text (enables token streaming) |
| `TextFinal(text)` | A complete assistant message or text block |
| `ToolAttempt(kind, detail)` | **Anything that suggests a tool, command, file, MCP, search, hook or subagent**, including an init event that lists available tools. Never put tool input in `detail`. |
| `UsageReport(input_tokens, output_tokens, cached_input_tokens, cache_write_tokens)` | Token usage; `input_tokens` is the *uncached* remainder |
| `Completed(final_text)` | The turn ended successfully |
| `ProviderFailure(kind, message, retry_after)` | The CLI reported an error; classify it (`RATE_LIMITED`, `AUTH`, `OVERLOADED`, `CONTEXT_LENGTH`, `MODEL_UNAVAILABLE`, `MAX_TURNS`, `PROTOCOL`, `UNKNOWN`) |
| `Ignored(event_type)` | Recognized but irrelevant (status, reasoning, ...) |

### Fail closed

Unknown event types are the dangerous case: a new CLI version may add a new
kind of tool. Use `looks_like_execution(type_name)` from
`modelmux.drivers.events`: any unknown type containing `command`, `exec`,
`tool`, `file`, `patch`, `mcp`, `search` or `shell` must become a
`ToolAttempt`. Emit `ToolAttempt` on the **earliest** signal (for example an
"item started" event rather than "item completed"), so the process is killed
before the action finishes.

## A complete minimal example

This is the driver used in the test suite (`server/tests/fakes/echo_driver.py`). It
treats every stdout line as the answer:

```python
from typing import ClassVar

from modelmux.drivers.base import (
    Driver,
    DriverRequest,
    Invocation,
    ModelInfo,
    ProbeContext,
    ProbeResult,
)
from modelmux.drivers.events import Completed, NormalizedEvent, TextFinal
from modelmux.errors import ModelMuxError


class EchoDriver(Driver):
    name: ClassVar[str] = "echo"
    binary_name: ClassVar[str] = "fake-cli"
    supported_versions: ClassVar[str] = ">=0"

    def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="echo-1", cli_model="echo-1")]

    async def probe(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(ok=True, reason="ok")

    def build_invocation(self, req: DriverRequest) -> Invocation:
        return Invocation(argv=self.argv("--model", req.cli_model), stdin=req.transcript.encode())

    def parse_line(self, line: bytes) -> list[NormalizedEvent]:
        return [TextFinal(line.decode("utf-8", "replace")), Completed()]

    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None:
        return None
```

A real driver's probe should look like the Claude driver's
(`server/src/modelmux/drivers/claude/driver.py`):

```python
async def probe(self, ctx: ProbeContext) -> ProbeResult:
    # 1. version:  ctx.run(lambda ws: Invocation(self.argv("--version"), b""), budget=30)
    # 2. flags:    run the real lockdown argv with EMPTY stdin; the CLI must fail
    #              with its "no input" message, not "unknown option".
    # 3. live:     one tiny real request built with build_invocation(); require
    #              Completed and no ToolAttempt / ProviderFailure.
    ...
```

## Registering the driver

Built-in drivers are listed in `server/src/modelmux/drivers/registry.py`. A
third-party package registers an entry point instead:

```toml
# pyproject.toml of your package
[project.entry-points."modelmux.drivers"]
mycli = "my_package.driver:MyDriver"
```

Install the package next to ModelMux and set `MODELMUX_DRIVER=mycli`. Only the
selected entry point is imported. The class is validated at startup: required
attributes, a valid `supported_versions`, a matching `name`, and a non-empty
model list with valid, unique IDs.

> A selected driver runs with ModelMux's full privileges. Only install drivers
> you trust.

## Testing a driver

The test suite gives you most of what you need:

- **Record real output** as JSONL fixtures under `server/tests/fixtures/<driver>/`
  (plain text, an error, and, carefully, one tool attempt), and hand-write
  the rest from the CLI's documentation or source. Note which are which in a
  `README.md` next to them.
- **Parser tests** over the fixtures, including garbage lines and deep nesting.
- **The fake CLI** (`server/tests/fakes/fake_cli.py`) can replay any fixture
  (`FAKE_SCENARIO=fixture`, `FAKE_FIXTURE=...`), hang, flood output, ignore
  SIGTERM, leave children behind, and record its argv and environment. Teach it
  your CLI's `--version`, "unknown flag" and "no input" behaviour for probe tests.
- **The security suite** (`server/tests/security/test_api_security.py`) is
  parametrized over drivers: add yours to `driver_name` and to the per-driver
  maps in `server/tests/helpers.py`.
