# TODO

## Next iteration

- [ ] Implement `--acp` backend (Agent Client Protocol — persistent
      `copilot.exe` process driven over JSON-RPC on stdin/stdout). Goal:
      drop turn time from ~8s to roughly the api_ms floor (~3s) by
      eliminating per-turn process spawn + MCP boot + shutdown.

## Research / evaluation

- [ ] Evaluate competing CLI tools as backend candidates:
    - `claude-code` (Anthropic) — designed for `-p` automation, exposes
      `--output-format stream-json` for stdin/stdout JSON I/O. Likely the
      strongest persistent-wrapper target.
    - `qwen3-code` — Qwen's coding-agent CLI. Unknown automation surface.
    - `antigravity-cli` — brand-new (May 2026). Check what's there.
    - `codex` (OpenAI) — official CLI; check whether it has a
      programmatic / streaming mode beyond interactive TUI.
  For each: spawn cost, persistent mode availability, output format,
  model selection, auth model. Pick the strongest fit and either pivot
  the proxy or run multiple backends side-by-side.

## Polish (lower priority)

- [ ] Inject "Be terse; no commentary" instruction server-side for
      chat-mode prompts to suppress gpt-5-mini's preamble/postamble.
- [ ] Surface usage stats (premiumRequests, api_ms) in the UI's per-turn
      meta line, not just in the launcher console.
- [ ] Investigate the `<system_reminder>` / `<sql_tables>` injection that
      copilot prepends to every user prompt. Coming from somewhere in
      ~/.copilot/ — find and disable for chat use.
