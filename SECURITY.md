# Security

ModelMux runs AI coding agents, programs built to execute commands and edit
files, as plain text models. Its whole job is to make sure they cannot. This
document describes the threat model, every control and how it is verified.

## Reporting a vulnerability

Please report security issues privately through
[GitHub security advisories](https://github.com/smit153/modelmux/security/advisories/new)
rather than a public issue. Include a reproduction if you can. You will get an
acknowledgement within a few days.

## Trust boundaries

```
 untrusted                     trusted                          untrusted
┌──────────────┐   HTTPS/   ┌───────────────────────────────┐   argv + stdin  ┌───────────┐
│ API clients  │──bearer──▶ │ ModelMux (FastAPI + runtime)  │───────────────▶ │ CLI proc  │──▶ provider
│ message text │            │ validates, renders, supervises│ ◀── JSONL ───── │ (model +  │
└──────────────┘            └───────────────────────────────┘   stdout        │  agent)   │
                                                                              └───────────┘
```

- **Request content is untrusted**: message text, tool schemas, IDs and every
  parameter. It is data for the model, never instructions for ModelMux.
- **The CLI and the model are untrusted**: prompt injection can make the model
  *try* anything. The CLI's output is parsed defensively and any sign of
  execution is fatal to the run.
- **The CLI binary itself is trusted** (pinned, checksum-verified) but confined.

## Threat model

Test names and paths in the last column are relative to `server/`.

| Threat | Mitigation | Verified by |
|---|---|---|
| Prompt injection makes the CLI run tools, commands or read files | All tools disabled by CLI flags (Claude: `--tools ""`, `--disallowedTools` for every built-in, `--restricted`, `--safe-mode`, empty strict MCP config, `--permission-mode dontAsk --permission-prompts none`; Codex: `--sandbox read-only`, approval `never`, 25 tool features disabled, web search disabled, no MCP) · **fail-closed tripwire** kills the process group on any tool event · empty private workspace · read-only container · egress restriction | `tests/security/test_api_security.py`, `tests/unit/test_*_parser.py`, `tests/integration/test_*_driver.py` |
| A CLI update silently removes a lockdown flag | Startup probe checks **every** flag (and, for Codex, every feature name and config key) is accepted; the service refuses to start otherwise | `test_probe_missing_flag`, `test_probe_unrecognised_config_key` |
| Codex silently ignores misspelled `-c` config keys | The probe proves each key is recognised by passing an invalid value and expecting an error that names it | `test_probe_unrecognised_config_key` |
| Role spoofing inside message content (`[SYSTEM]`, fake markers) | Per-request random boundary (`MMX-` + 64 random bits); only markers with that exact boundary are real; regenerated if it ever appears in content | `test_role_spoofing_*` |
| Argument / flag injection via `model` or other fields | Only allowlisted model values reach argv; values starting with `-` are rejected at config load; no other request data is ever placed in argv (prompt via stdin, system prompt via a `0600` file) | `test_model_injection_never_reaches_argv`, `test_no_request_data_in_argv` |
| Shell injection | No shell anywhere; argv lists only; one module may spawn processes | ruff banned-API rule + `tests/security/test_source_scan.py` |
| Secret leakage via environment | Environment built from scratch: `PATH`, `HOME`, locale, `TZ` and a per-driver allowlist; `MODELMUX_*` can never be passed; provider keys in ModelMux's env are **not** forwarded | `test_env_contains_only_allowlisted_keys` |
| Secret / content leakage via logs | Logs never contain prompts, completions, tool arguments, headers, argv or env by default; a redaction filter masks bearer tokens, `sk-`/`sk-ant-` keys, JWTs and configured keys, including in tracebacks; repair reasons (which can quote model output) are not logged | `test_default_logs_have_no_secrets_or_content`, `tests/unit/test_logging.py` |
| Leakage via error responses | Errors carry a fixed public message; stderr, exit codes, paths, argv and env go to logs only | `test_errors_never_leak_internals` |
| Resource exhaustion | Body, prompt, message, tool, schema, argument and stop limits (checked before any process starts) · concurrency semaphore + bounded queue with `Retry-After` · first-output / idle / total timeouts · stdout line and total caps, stderr ring buffer · container CPU, memory and PID limits | `test_limits_enforced_before_spawn`, `tests/integration/test_runner.py` |
| Deeply nested JSON (recursion) | Request bodies, CLI output lines and model replies that exceed Python's recursion limit are rejected, never crash the request | `test_deeply_nested_*` |
| Orphaned or zombie processes | Each CLI runs in its own session; on any exit path the group gets SIGTERM, then SIGKILL after `KILL_GRACE`; the group is SIGKILLed even after a clean exit; cleanup waits for death even when the request is cancelled; `tini` is PID 1 | `test_group_kill_escalates_to_sigkill`, `test_children_killed_after_clean_exit`, `test_cancel_during_kill_waits_for_death`, `test_disconnect_*` |
| Unauthorized use | Mandatory bearer keys (≥ 32 chars), compared in constant time against every key; ports published on `127.0.0.1` only | `tests/unit/test_auth.py` |
| Malicious third-party driver plugin | Only the driver named in `MODELMUX_DRIVER` is imported; installed but unselected plugins are never loaded. **A selected plugin runs with ModelMux's full privileges**: install only drivers you trust | `tests/unit/test_registry.py` |
| Supply chain | Locked Python dependencies (`uv.lock`); CLI versions pinned in `docker/cli/package-lock.json` with sha512 integrity, verified by `npm ci`; base images pinned by digest; Debian security updates at build; vulnerability scan and SBOM in CI | `.github/workflows/ci.yml` |

## The tripwire

The runtime passes every CLI output line to the driver's parser. Any
`ToolAttempt` event makes the pipeline kill the process group immediately and
return `502 sandbox_violation` (nothing is streamed after it).

**Claude Code** trips on:
- an `init` event that lists **any** tools or MCP servers,
- a `content_block_start` of any type other than text or reasoning (for
  `tool_use` this arrives before the tool input is complete),
- tool input deltas, `tool_result` messages, subagent events, hook events,
  permission denials, server-side tool use,
- a `commands_changed` event that lists any commands,
- any unknown event or block whose type contains `command`, `exec`, `tool`,
  `file`, `patch`, `mcp`, `search` or `shell`.

**Codex** trips on `item.started` (not only `item.completed`) for
`command_execution`, `file_change`, `mcp_tool_call`, `collab_tool_call`,
`web_search`, and on any unknown execution-like item or event type.

The tool name may appear in logs; tool input never does.

## Container hardening

| Control | Setting |
|---|---|
| User | UID/GID 10001, no login shell |
| Root filesystem | read-only; writable only `/tmp` (tmpfs, `noexec,nosuid,nodev`) and the driver-home volume |
| Capabilities | all dropped; `no-new-privileges` |
| setuid/setgid binaries | none (stripped at build) |
| PID 1 | `tini` (signal forwarding, zombie reaping) |
| Resources | `pids_limit`, `mem_limit`, `cpus` in compose |
| Network | ports on `127.0.0.1`; restrict egress to the provider's domains |
| Mounts | only the per-driver driver-home volume; never the Docker socket or host directories |

## Vulnerability policy

CI builds the image and runs Trivy. It **fails on HIGH/CRITICAL findings that
have a fix available** (`--ignore-unfixed`); Debian security updates are
applied at build time, so fixable issues are fixed by rebuilding. Findings the
distribution has not fixed yet are reviewed on each release. Exceptions go in
`.trivyignore`, each with a justification and a review date. A CycloneDX SBOM
is attached to every CI run.

## The `modelmux` CLI

The CLI controls Docker on the user's machine, so it is kept small and strict:

- **No dependencies** beyond the Python standard library.
- **No shells**: one module starts processes (`docker`), always with argument
  lists and timeouts; a test scans the source to enforce it.
- **Secrets**: the client API key is generated with `secrets`, written
  atomically with mode `0600` (directory `0700`), never printed unless the
  user runs `modelmux key show` or `config --reveal-key`, and masked in all
  other output including `--verbose`. Login keystrokes, pasted codes and
  provider API keys are relayed to the provider CLI and never stored or logged.
- **Containers**: the generated compose file and every helper container are
  read-only, non-root, without capabilities and with `no-new-privileges`;
  ports are published on `127.0.0.1` only; there are no host-folder mounts and
  never the Docker socket. Each provider's login has its own volume.
- **Images**: each CLI release runs the image built in the same release,
  pinned by digest, never `latest`. Releases publish with PyPI trusted
  publishing (no stored tokens) and attach provenance and an SBOM.

## Known limitations

- ModelMux cannot see network traffic the CLI makes to its provider;
  restricting egress at the network level is the operator's responsibility.
- The model can still *say* anything, including attempts to persuade your
  application to run dangerous tools. Simulated tool calls are executed by your
  client: validate them there as you would any untrusted input.
- A successful Codex response has not been observed live (no account during
  development); the tripwire for Codex is built from the Codex source and
  tested with hand-written fixtures.
