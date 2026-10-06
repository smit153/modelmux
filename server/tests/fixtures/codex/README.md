# Codex `exec --json` fixtures

There is no Codex account for this project, so no successful run was
recorded. Event and item shapes follow codex-rs `exec/src/exec_events.rs`
(`ThreadEvent`, `ThreadItemDetails`, `Usage`).

Recorded from codex-cli 0.159.2 (no login; cf-ray, request and thread IDs
replaced):

- `auth_error.jsonl`: `Reconnecting...` retry notices, a non-fatal `error`
  item (transport fallback), then `turn.failed` with 401

Hand-written from the source schema:

- `text`, `reasoning_todo`, `no_usage`, `no_completion`, `malformed`
- tripwire: `command_execution`, `file_change`, `mcp_tool_call`, `web_search`,
  `collab_tool_call`, `unknown_exec_item`, `unknown_exec_event`
- failures: `rate_limited`, `usage_limit`, `context_too_long`, `overloaded`,
  `model_not_found` (message texts are plausible, not captured)

Model discovery:

- `model_catalog.json`: `codex debug models --bundled` from codex-cli 0.159.2,
  trimmed to `slug`, `display_name`, `visibility`, `supported_in_api` and
  `priority` (the real output also carries prompts and settings, ~650 KB)
