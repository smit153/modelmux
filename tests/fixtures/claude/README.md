# Claude Code stream-json fixtures

Recorded from Claude Code 2.1.285 with `--model sonnet` (session IDs,
UUIDs, timestamps and local paths replaced with fixed values):

- `text_partial.jsonl`: plain reply with `--include-partial-messages`
  (run with a planted CLAUDE.md and hook; both were ignored)
- `model_not_found.jsonl`: invalid model name (API error, zero cost)
- `tool_use_read.jsonl`: one-off capture with `--tools Read` in an empty
  directory: `content_block_start` tool_use, `tool_result`, `error_max_turns`

Hand-written, following the recorded shapes (not captured live):

- `rate_limited`, `auth_error`, `overloaded`, `context_too_long`: synthetic
  assistant API-error message plus an `is_error` result
- `rate_limit_rejected`: `rate_limit_event` with status `rejected`
- `init_with_tools`, `init_with_mcp`, `hook_event`, `unknown_exec_event`,
  `server_tool_use`, `permission_denied`, `subagent_event`: tripwire cases
- `thinking`, `no_usage`, `no_completion`, `malformed`: parser edge cases
