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
- [x] Antigravity backend evaluation (2026-05-26, `spike/antigravity`
      deleted, nothing committed). Two Windows-native paths considered:
    - `google-antigravity` Python SDK: **blocked**. v0.1.0 wheels exist
      for mac-arm64 / linux-x86_64 / linux-aarch64 only — no Windows
      wheel. The Go `localharness` source is not in the public repo
      (only the generated `localharness_pb2.py` and a Kokoro release
      script referencing an internal Google-on-Borg commit), so a
      source build isn't an option. Re-check PyPI for a Windows wheel
      later.
    - `agy.exe` CLI 1.0.2 (`C:\Users\wossn\AppData\Local\agy\bin\`):
      works. Cold `-p` ~5.5s per turn (already half the overhead of
      `copilot -p`). Warm via `--conversation=<id>` or `-c`: ~3.9-4.1s,
      i.e. ~28% saved per turn (vs ~70% saved by the copilot wrapper).
      And `-p` with a conversation reprints the **entire transcript**
      on every call, not just the new assistant turn — extracting
      deltas would be parsing-fragile.
  Architecture note worth keeping: `agy.exe` is a 144 MB self-contained
  Go binary with ~1.5k WebSocket references and zero `localharness`
  symbols. Almost certainly embeds its own harness internally, which
  the in-binary REPL talks to over a local WebSocket loop — same
  pattern the SDK uses with the separate `localharness` subprocess.
  So Google ships three productizations of the same idea: SDK
  (Python + subprocess harness), `agy` CLI (frontend + embedded
  harness), and presumably the Antigravity IDE. Agentry's reverse-MCP
  framing applies cleanly. Verdict: shelved. Re-evaluate when (a)
  Google publishes a Windows wheel for `google-antigravity` — at
  which point the in-process SDK obsoletes the wrapper anyway — or
  (b) `agy` grows a streaming / JSON delta output mode.

  First-run gotcha: `agy.exe` does OAuth on first invocation via
  a browser popup. Headless sessions (e.g. CI, automated harnesses)
  hang silently with 0% CPU because the consent never completes;
  run interactively once to authenticate, then the same commands
  work non-interactively. There's no `agy login` subcommand.

## Research / evaluation

- [ ] Evaluate competing CLI tools as backend candidates:
    - `claude-code` (Anthropic) — designed for `-p` automation, exposes
      `--output-format stream-json` for stdin/stdout JSON I/O. Check
      whether it speaks ACP yet. Likely the strongest persistent-wrapper
      target.
    - `qwen3-code` — Qwen's coding-agent CLI. Unknown automation surface.
    - ~~`antigravity-cli`~~ — done; see entry below.
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
