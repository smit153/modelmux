# Recorded login screens

Captured on 2026-10-05 from the ModelMux image (Claude Code 2.1.285,
codex-cli 0.159.2) through a real pseudo-terminal, in throwaway containers
with temporary volumes, cancelled before any code was entered. Single-use
values were replaced: the OAuth `state` and `code_challenge` (Claude) and the
device code (Codex).

| File | Command |
|---|---|
| `claude_browser.raw` | `claude auth login`: OSC 8 hyperlink + colour codes around the URL, then `Paste code here if prompted >` |
| `codex_device.raw` | `codex login --device-auth`: device URL and one-time code, with colour codes |
