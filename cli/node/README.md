# modelmux-cli

Set up, log in to and run [ModelMux](https://github.com/smit153/modelmux):
AI coding CLIs (Claude Code, Codex) as a secure, OpenAI-compatible API on your
own machine.

```bash
npm install -g modelmux-cli    # or run it without installing: npx modelmux-cli
modelmux up                    # set up Docker containers, start logged-in providers
modelmux login claude          # guided login (opens your browser), then a real test
modelmux config litellm        # ready-to-paste config for LiteLLM, OpenAI SDK, LangChain...
```

Other commands: `status`, `logs`, `doctor`, `upgrade`, `logout`, `key show`, `down`.

- Requires Docker with Compose v2 (Docker Desktop on macOS and Windows) and
  Node.js 22+. No Python, and no other dependencies.
- The same CLI as the Python package `modelmux-cli` on PyPI: same commands,
  messages and settings, so either one can manage the same setup.
- Each CLI release runs the server image built in the same release, pinned by
  digest.
- Logins live in per-provider Docker volumes; the API key is generated
  locally and stored with owner-only permissions.

Full documentation: <https://github.com/smit153/modelmux/blob/main/docs/CLI.md>
