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
- [x] Backend protocol refactor (2026-05-30, `feat/codex-backend`):
      extracted a `Backend` ABC into `backends.py`; `CopilotACPBackend` is
      the original ACPClient moved verbatim (zero behavior change),
      `CodexAppServerBackend` is new. Select via `--backend {copilot,codex}`
      (default `copilot`). Shared logging in `logutil.py`. Both backends
      verified end-to-end through the Flask layer.
- [x] Codex backend (`codex app-server`, JSON-RPC 2.0 over stdio).
      Validated 2026-05-30: default `gpt-5.4-mini` @ `low` effort, median
      TTFB ~6.5s — beats the Copilot SHORT baseline (7.56s). Auth is the
      ChatGPT account login (no API key). Streams reasoning traces
      (`item/reasoning/textDelta`), which Copilot does not. Paid-cheap tier
      (ChatGPT Go $8 / Plus $20). See `archive/CODEX-PLAN.md`, `_bench/codex_probe.py`.
- [x] Claude Code backend (`claude -p`, COLD-START). Landed 2026-05-31 as the
      premium tier. claude-code has no persistent stdio server, so it spawns a
      fresh process per turn: ~2.5s startup overhead (Sonnet 4.6, lean config),
      which buys zero cross-turn context bleed — the right trade for independent
      enrichment tasks (40–90s each). Lean config (`--strict-mcp-config` +
      `--disallowedTools` + empty scratch cwd) ~halves cold start vs default.
      Default model `claude-sonnet-4-6`. Persistent mode (~1.3s/turn) deferred —
      see `archive/CLAUDE-PLAN.md` for why prompting can't cleanly fix its
      cross-turn leakage. `_bench/claude_probe.py`.
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

**Updated 2026-05-30** — codex landed as backend #2 (see "Done"). The
backend interface now exists, so adding further backends is incremental
rather than a refactor. Remaining candidates are still deferred (see
`TODONT.md` "Multi-backend support" — now narrowed): the audience that
would prefer them already has paid tooling.

- [ ] Evaluate remaining CLI tools as backend candidates (low priority):
    - ~~`claude-code` (Anthropic)~~ — DONE, landed as cold-start backend #3
      (2026-05-31). It IS `-p`-per-turn with no persistent protocol, but the
      ~2.5s cold start is fine against 40–90s enrichment turns, and the
      isolation is a feature here. See "Done" / `archive/CLAUDE-PLAN.md`.
    - `qwen3-code` — Qwen's coding-agent CLI. Unknown automation surface.
    - ~~`antigravity-cli`~~ — evaluated, shelved (see "Done").
    - ~~`codex` (OpenAI)~~ — DONE, landed as a backend.
  For any new candidate: implement the `Backend` ABC in `backends.py`,
  wire it into `make_backend`, add to the `--backend` choices.

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
- [ ] (codex) Only if the ~24.8k base harness ever becomes a real
      constraint: try `baseInstructions` on `thread/start` to REPLACE
      codex's base system prompt (vs `developerInstructions`, which
      appends). It's the one lever that shrinks the prefix, but it
      discards codex's agent scaffolding — a behavior change, not a free
      win. Untested. See archive/CODEX-PLAN.md base-context note. Today the
      harness is cached/free, so not worth it.

## Architecture

- [x] Factor backend out of `agentry.py` into a plugin interface so a
      second CLI can live as a sibling to the copilot backend. Done
      2026-05-30 — `Backend` ABC + `make_backend` factory in `backends.py`;
      codex validated the design end-to-end.
- [x] README "Reverse MCP" section — frames the proxy as the inversion
      of MCP's consumer/provider roles (MCP makes tools callable by
      models; agentry makes a model callable by code, and enforces the
      flip via the `-32601` tool-deny). Done 2026-05-31.
