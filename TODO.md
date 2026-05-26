# TODO

## Done

- [x] `--acp` backend (persistent `copilot.exe` driven over JSON-RPC on
      stdio). Turn time dropped from ~8 s (`-p` mode) to ~2-3 s (api_ms
      floor) for short prompts.
- [x] Reasoning-effort dropdown in the web UI; per-request override
      applied via `session/set_config_option`.
- [x] Local `.github/copilot-instructions.md` to suppress global
      `<system_reminder>` / `<sql_tables>` bleed into prompts and ask
      for terse, chat-only replies.
- [x] Rename `copilot-proxy` → `agentry`; repo published to
      `github.com/aweussom/agentry`.
- [x] Linux / WSL2 launcher (`start.sh`) + `.gitattributes` for LF.
- [x] README with persistent-wrapper pitch, Windows + Linux quick start,
      architecture overview, and known limits.

## Research / evaluation

- [ ] Evaluate competing CLI tools as backend candidates:
    - `claude-code` (Anthropic) — designed for `-p` automation, exposes
      `--output-format stream-json` for stdin/stdout JSON I/O. Check
      whether it speaks ACP yet. Likely the strongest persistent-wrapper
      target.
    - `qwen3-code` — Qwen's coding-agent CLI. Unknown automation surface.
    - `antigravity-cli` — brand-new (May 2026). Check what's there.
    - `codex` (OpenAI) — official CLI; check whether it has a
      programmatic / streaming mode beyond the interactive TUI.
  For each: spawn cost, persistent mode availability, output format,
  model selection, auth model. Pick strongest fit and either pivot the
  proxy or run multiple backends side-by-side.

## Polish (lower priority)

- [ ] Surface usage stats (premiumRequests, api_ms, tokens) in the UI's
      per-turn meta line, not just in the launcher console.
- [ ] Add a model dropdown to the web UI. Currently only reasoning
      effort is per-request; model is fixed at launch via `--model`.
- [ ] Probe whether `xhigh` / `max` reasoning levels work via ACP
      `set_config_option`. CLI accepts them but they're not in the
      session's published options; might still round-trip.
- [ ] Consider selective tool permissions instead of blanket `-32601`
      deny. Currently every agent->client request is rejected, so any
      prompt that genuinely needs a tool fails rather than degrading.

## Architecture (only if multi-backend pans out)

- [ ] Factor backend out of `agentry.py` into a plugin interface so a
      second CLI (claude-code etc.) can live as a sibling to the
      copilot backend. Premature until at least one second backend is
      validated end-to-end.
- [ ] README "Reverse MCP" section — frame the proxy as the inversion
      of MCP's consumer/provider roles (MCP turns tools into LLM
      services; agentry turns LLMs into HTTP services). Conceptual
      hook, not a feature.
