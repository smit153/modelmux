# Errors

Every error has the OpenAI shape and carries the `X-Request-ID` header:

```json
{"error": {"message": "...", "type": "invalid_request_error", "code": "model_not_found", "param": "model"}}
```

`message` is a fixed, safe text (or names the offending field). Internal
details such as stderr, exit codes, paths, argv and environment are only
written to the logs, keyed by the request ID.

> `tests/unit/test_docs.py` fails if an error code is missing from this page.

| Code | HTTP | OpenAI `type` | Retry? | When |
|---|---|---|---|---|
| `invalid_request` | 400 | `invalid_request_error` | no | Schema or validation failure (invalid JSON, wrong field, `n > 1`, bad tool schema, bad `tool_choice`) |
| `unsupported_parameter` | 400 | `invalid_request_error` | no | Legacy `functions` / `function_call` / role `function`, `logprobs`, non-`function` tools |
| `unsupported_content` | 400 | `invalid_request_error` | no | Image, audio or file content parts |
| `authentication_failed` | 401 | `authentication_error` | no | Missing or invalid bearer key |
| `model_not_found` | 404 | `invalid_request_error` | no | Model not in the allowlist, or the provider says the model does not exist |
| `not_found` | 404 | `invalid_request_error` | no | Unknown route (including `/docs` when disabled and `/v1/responses`) |
| `method_not_allowed` | 405 | `invalid_request_error` | no | Wrong HTTP method on a known route |
| `payload_too_large` | 413 | `invalid_request_error` | no | Body or a tool definition over its limit |
| `context_too_large` | 413 | `invalid_request_error` | no | Rendered prompt over `MAX_PROMPT_BYTES`, or the provider reports context length exceeded |
| `unsupported_media_type` | 415 | `invalid_request_error` | no | `Content-Type` is not `application/json` |
| `provider_rate_limited` | 429 | `rate_limit_error` | yes | The CLI reports a rate or usage limit (`Retry-After` when known) |
| `internal_error` | 500 | `server_error` | no | Unexpected exception (full traceback in the logs) |
| `provider_auth_error` | 502 | `server_error` | no | The CLI reports its provider credentials are invalid or missing |
| `provider_error` | 502 | `server_error` | yes | Non-zero exit or unclassified CLI failure |
| `protocol_error` | 502 | `server_error` | yes | Unparseable output, or the CLI exited without completing |
| `invalid_model_output` | 502 | `server_error` | yes | Tool call or structured output still invalid after the single repair |
| `output_too_large` | 502 | `server_error` | no | Output line or total output over its cap |
| `sandbox_violation` | 502 | `server_error` | no | The tripwire fired: the CLI tried to use a tool; the process was killed |
| `overloaded` | 503 | `server_error` | yes | Queue full or queue wait timed out (`Retry-After: 5`) |
| `provider_unavailable` | 503 | `server_error` | yes | Provider overloaded or unreachable |
| `driver_not_ready` | 503 | `server_error` | yes | Reserved. With fail-fast startup the service exits instead of serving while the driver is unusable |
| `provider_timeout` | 504 | `server_error` | yes | First-output, idle or total timeout |

## Which error wins

When a run ends, the error is chosen in this order:

1. A tripwire violation, timeout or output cap that already stopped the run.
2. Any failure the CLI reported (rate limit, auth, overload, context length...).
3. A non-zero exit code, classified by the driver (default `provider_error`).
4. Exit code 0 without a completion event: `protocol_error`.

ModelMux does not retry provider calls itself (the gateway in front should).
The only re-run is the single repair for invalid tool or structured output.

## Streaming

- Errors **before the first chunk** are normal HTTP errors, as above.
- Errors **after streaming started** arrive as one event, then the stream ends:

  ```
  data: {"error": {"message": "...", "type": "server_error", "code": "provider_unavailable", "param": null}}

  data: [DONE]
  ```

- If the client disconnects, the CLI process group is killed and the access log
  records status `499`.

## Health endpoints

`/health/live` returns `200 {"status":"ok"}` while the process responds.
`/health/ready` returns `200 {"status":"ready"}`, or `503` with
`{"status":"not_ready","reason":"driver_not_ready"|"saturated"}` (before the
startup probe finishes, or when every slot is busy and the queue is full).
