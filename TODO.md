# TODO

## Done

- [x] Grok Build backend (`grok agent stdio`, ACP) landed 2026-10-04 as
      backend #4, SuperGrok / X Premium+ tier. One ACP session per chat,
      model/effort as session state inside the turn lock, live text and
      thought streaming, `session/cancel`, inline `image_gen` images. Tool
      lockdown is `grok-agent-profile.md` because `grok agent` takes none
      of the headless tool flags. Default `grok-4.7 @ high`; the 2x-price
      fast variant is hidden. `archive/GROK-PLAN.md`, `_bench/grok_acp_probe.py`,
      `test_grok_backend.py`.

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
- [x] Codex backend: stop hardcoding the model (2026-07-27). OpenAI deprecated
      `gpt-5.4-mini` (migration target `gpt-5.6-luna`), which would have broken
      every `turn/start` once removed. `CodexAppServerBackend` now defaults
      `model=None` — the turn-level override is omitted and each thread runs on
      codex's own configured model (`~/.codex/config.toml`, i.e. the last TUI
      selection). `--model` still pins explicitly. Probed `gpt-5.6-luna` @ `low`
      via `_bench/codex_probe.py`: median TTFB 6.20s (vs 6.5s on 5.4-mini), and
      verified end-to-end through Flask on a test port. Trade-off documented in
      README: switching models in the codex TUI now silently changes what
      agentry uses on its next thread.
- [x] Claude Code backend (`claude -p`, COLD-START). Landed 2026-05-31 as the
      premium tier. claude-code has no persistent stdio server, so it spawns a
      fresh process per turn: ~2.5s startup overhead (Sonnet 4.6, lean config),
      which buys zero cross-turn context bleed — the right trade for independent
      enrichment tasks (40–90s each). Lean config (`--strict-mcp-config` +
      `--disallowedTools` + empty scratch cwd) ~halves cold start vs default.
      Default model `claude-sonnet-4-6`. Persistent mode (~1.3s/turn) deferred —
      see `archive/CLAUDE-PLAN.md` for why prompting can't cleanly fix its
      cross-turn leakage. `_bench/claude_probe.py`.
      **2026-09-11:** leakage objection removed — `/clear` over stream-json
      resets the conversation in place; see the completed persistent-claude item below.
- [x] OpenAI-like model handling (2026-08-13, after the GitHub AI-credits
      billing switch made model choice a per-token cost decision):
    - Per-request `model` honored on `/v1/chat/completions` (copilot switches
      the live session via `session.set_model`; codex/claude pin the next
      turn/spawn). Unknown copilot models get an OpenAI-style 404
      `model_not_found`.
    - Truthful reporting: backends expose `current_model()` (verified against
      the runtime's `get_current`, resolves codex's config.toml default), so
      response `model` fields, `X-Model`, and `/v1/models` no longer lie.
      `/v1/models` lists the real account model set with price categories.
    - Full reasoning-effort vocabulary forwarded (none..max); UI dropdown +
      model picker expanded to match.
    - AI-credit cost tracking: per-turn credits from `AssistantUsageData`
      (`totalNanoAiu`, 1e9 = 1 credit) logged per turn and shown as
      `this run N.NN`. NB: copilot-cli's exit banner prints a *session* total
      that can span a month boundary (its 123.23 was 58.89 on Aug 31 plus 64.34
      on Sep 1), so it will not match the month figure — hence the deliberate
      `this run` wording.
    - Account-wide credit allowance (2026-09-01): `account/getQuota` IS in the
      SDK (`client.rpc.account.get_quota`). Field names mislead —
      `premium_interactions.used_requests` is **AI credits**, not a request
      count (read 65 against both the local ledger's 65.44 and
      github.com/billing's own "65 / 5,000 AI credits"), and `reset_date` is
      **useless**: it echoes the request timestamp, not a reset instant (two
      probes 20s apart each came back stamped with their own call time). The
      period is the **calendar month**, resetting on the 1st. Don't misread the
      billing page's "resets in 30 days on Sep 30, 2026" as a rolling window:
      September just has 30 days, and Sep 30 is the last covered day. Proof it
      isn't rolling — a window ending Sep 30 would start Aug 31 and include
      that evening's 58.89 credits, so `used` would read ~124, not 65. See
      `_bench/quota_probe.py`.
    - Heartbeat legibility (2026-09-01): the first cut printed
      `plan 65/5000 (1% used) · machine 65 AIC this month` — two labels for the
      same credits over the same window, in a unit ("machine AIC") that means
      nothing to a reader. Now one authoritative line,
      `Copilot credits 70/5,000 used this month · 4,930 left`, with the local
      ledger demoted to a clearly-worded fallback for when the RPC is silent.
      Refresh moved off `prompt()` onto a 120s TTL driven from
      `quota_status()` (`_plan_quota_line()`) plus a startup prime, because the
      heartbeat is an *idle* display: the per-turn refresh left it blank until
      the first turn ever ran, which is when it is most looked at.
    - Copilot default model: gpt-5-mini → `gpt-5.6-luna` (cheapest band,
      $0.20/M in; prompt caching verified working on 5.6 — turn 2+ bills
      ~10× less; see `_bench/copilot_sdk_probe.py`, which replaces the
      retired `--acp` bench.py).
- [x] Codex backend refresh (2026-08-13, codex-cli 0.147.0 / app-server v2
      era). Probed live (`_bench/codex_usage_probe.py`) + web research:
    - **Reasoning streamed**: `item/reasoning/summaryTextDelta` now yielded
      as ("reasoning", text) — the web UI thinking block works on codex.
      Requires `config: {model_reasoning_summary: "detailed"}` on
      thread/start (verified: without it, NO reasoning notifications fire
      at any effort).
    - **Usage/cost accounting**: per-turn tokens from
      `thread/tokenUsage/updated` + a Codex-credits estimate from the
      official rate card (April 2026 token-aligned scheme: sol 125/750,
      terra 50/300, luna 5/30 credits per 1M in/out; ~$0.04/credit) logged
      per turn and summed per session. Plus-plan quota is currently
      weekly-window only (5h bucket suspended July 2026); credits balance
      and banked rate-limit resets surface in `account/rateLimits/read`.
    - **model/list wired in**: `/v1/models` lists codex's real models,
      update_model validates against it (404 on unknown ids). New effort
      levels max/ultra + "fast" service tiers exist; ultra added to the
      effort vocabulary.
    - **Spawn fix**: npm ships codex only as .cmd/.ps1 shims now —
      resolve via shutil.which() or Popen dies with WinError 2.
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

  **Re-evaluated 2026-07-28** — trigger (a) fired: `google-antigravity`
  0.1.8 (2026-07-23) ships `win_amd64`/`win_arm64` wheels; weekly release
  cadence since 0.1.0. Validated on Windows in a scratch venv: in-process
  `Agent` API (no subprocess JSON-RPC needed), `Agent.chat()` returning
  streamed `chunks`, `CapabilitiesConfig(enabled_tools=[])` for native
  tool-stripping (cleaner than our `-32601` refusals; write tools require
  an explicit safety policy), per-turn `UsageMetadata` incl. cached tokens,
  ~2.3s session startup. On paper: the Copilot-SDK story again.
  **New blocker: auth.** The SDK is API-key / Vertex only (it silently
  consumed a `GEMINI_API_KEY` from env and billed AI Studio prepay
  credits — 429 RESOURCE_EXHAUSTED on a depleted account). It cannot ride
  the sponsored/liberal `agy` subscription quota, which is the only thing
  worth unlocking — the Gemini API itself is directly purchasable by any
  client, so the SDK today fails the same bar that disqualified
  `qwen3-code`. STILL SHELVED. Watch: SDK issue #20 (OAuth / CLI-credential
  reuse — lands ⇒ copilot-SDK-style backend on sponsored quota) and CLI
  issue #31 (`--acp` on `agy` — lands ⇒ codex-style persistent wrapper).
  ~~Either one changes the answer.~~
  **Final verdict, same day: DECLINED — watch items dropped.** A same-day
  `agy -p` probe passed the remaining technical bars (stream-json,
  `--effort`, headless tool auto-deny), but the sponsored quota was gutted
  in March 2026 (~20 req/day free, weekly window) and Google has banned
  whole accounts for third-party subscription use. Economics + account
  risk kill it regardless of protocol. Full reasoning in `TODONT.md`.

  First-run gotcha: `agy.exe` does OAuth on first invocation via
  a browser popup. Headless sessions (e.g. CI, automated harnesses)
  hang silently with 0% CPU because the consent never completes;
  run interactively once to authenticate, then the same commands
  work non-interactively. There's no `agy login` subcommand.

- [x] **Copilot plan quota went stale** (found 2026-09-11 after a 1,485-call
      enrichment batch drained the month's 5,000 credits overnight; fixed same
      day). The heartbeat kept saying `190/5,000 used · 4,810 left` next to
      `this run 4809.53` because the runtime caches `account/getQuota` for its
      process lifetime — see TODONT. Fix: the snapshot is now a *baseline*,
      not a live figure. Its `resetDate` is the runtime's fetch timestamp, so
      `used_now = snapshot.used + ledger credits with created_at >= resetDate`
      (`_plan_quota_line`; `~` marks the estimate whenever the delta is
      non-zero; a baseline from a previous month is discarded in favour of the
      ledger's month-to-date). The periodic re-fetch (`_PLAN_QUOTA_TTL`, 15
      min) now spawns a *throwaway* runtime (`_fetch_quota_fresh`, ~4 s) so it
      actually observes other devices; the startup prime still uses the main
      runtime, which is brand new at that point. Hard-stop plans now read
      `exhausted, turns blocked until the 1st` instead of the wrong `billed
      as overage` (`usageAllowedWithExhaustedQuota` is false here).
      Filed upstream as [copilot-sdk#2619](https://github.com/github/copilot-sdk/issues/2619); repro
      `_bench/quota_cache_probe.py`. If it lands a TTL/force-refresh, drop the
      throwaway-runtime re-fetch and call the main client again.

- [x] **Codex image generation** (2026-09-12, codex-cli 0.154.0). codex ships
      it as a stable, default-on built-in tool (`image_generation` in
      `codex features list`), and it fires over `app-server` inside agentry's
      locked-down thread — approvalPolicy=never, sandbox=read-only, empty
      scratch cwd — with no approval round-trip (`_bench/codex_imagegen_probe.py`).
      The completed `imageGeneration` item carries the whole PNG as base64 in
      `result` (~0.7–1 MB) plus `revisedPrompt` and a `savedPath` under
      `~/.codex/generated_images/`. Landed:
    - `CHAT_ONLY_INSTRUCTIONS` carve-out: the flat "Do not use any tools"
      made codex refuse ("I'm unable to generate images in this chat");
      now image generation is the one permitted tool, "only when the user
      explicitly asks for an image", so enrichment prompts are unchanged.
    - Backend contract: `prompt()` yields `("image", mime, b64, revised)`;
      `/v1/chat/completions` emits it as `delta.images` / `message.images`
      (OpenRouter's convention, never in `content`); the web UI renders it
      under the reply. New `POST /v1/images/generations` in OpenAI Images
      shape (codex only, `n=1`, `b64_json`; size/quality ignored with a WARN
      — codex hardcodes gpt-image-2 @ auto/auto, see TODONT).
    - Measured: ~15–20 s per image; an image turn reports ~10 output tokens,
      so the "3–5× faster usage" OpenAI documents is a server-side weighting
      on the plan window, invisible to the token-based credits estimate.
      Three images moved a Plus 5h window ≤1 integer percentage point.
- [x] **Codex image edits** (2026-09-12, same day, prompted by a downstream
      user whose training-data pipeline needs every frame anchored on a
      reference image — "the reference carries the register, not the model").
      Probe `edit` mode: a 1536×1024 PNG attached as `ImageUserInput` + "recolor
      the shape to blue" → codex's tool fired in edit mode against the
      conversation image (no file on disk, no `imageView` step), returned a
      faithful recolor at **1536×1024** — the reference carries the
      proportions (not the orientation: a downstream 16:9 reference came back
      941×1672 when the prompt implied a standing figure), which narrows the
      size/quality TODONT. ~30 s, 1.3 MB here; ~45 s downstream with a long
      prompt and a 0.7 MB reference. That user's acceptance run also
      confirmed the register follows the reference (manga linework held with
      `canonical.png` attached, went semi-realistic without it). Landed
      `POST /v1/images/edits`: OpenAI multipart (`image`/`image[]` + form
      fields) or JSON with `image` as data: URI(s), ≤5 references, `mask`
      rejected 400. Shares `_run_image_turn` with `/generations`.
- [x] **Images `size` honored as aspect ratio** (2026-09-12, evening; Tommy
      doubted the "not controllable" verdict). Six trials
      (`_bench/codex_image_aspect_probe.py`): explicit orientation wording in
      the prompt pins the aspect every time under codex's hardcoded
      `size=auto`; the subject decides only when the prompt is silent (which
      is what flipped the downstream 16:9 reference to portrait). Pixel count
      is fixed at ~1.57 MP (1536×1024, 1254², 1672×941 for 16:9), and the
      assistant's prose claims the asked-for size regardless. Landed: `size`
      → aspect sentence in the wrapper, `data[].size` from the PNG IHDR, WARN
      on a >5% ratio miss, "call the tool exactly once" (a wordy dimensions
      instruction had made it call twice). TODONT entry updated (REVERSED on
      aspect, held on pixels/quality).
- [x] **Does the chat model matter for images?** (2026-09-12, Tommy asked.)
      No, for the picture: all five account models get the same `gpt-image-2`
      tool, all honored 1536×1024, 40–58 s each, one call each
      (`_bench/codex_image_model_probe.py`). Yes, for the *prompt*: the chat
      model rewrites it before the tool call — `luna` near-verbatim, `terra`/
      `sol`/`astra` ~3× longer with invented constraints (`sol`: "no weapon
      required" → sword gone). Keep `luna` (the default) for image work; it is
      the cheapest and the most faithful. Gating from `spec_plan.rs`: feature
      on, plan ≠ Free, model has image input, ChatGPT-login auth (API-key
      login does not get the tool).

## Backends

- [x] (grok) Images routes on grok (2026-10-04, same day): throwaway ACP
      session per call, `image_gen` for generations, `image_edit` with
      references written to the scratch cwd for edits (ACP takes no image
      input). JPEG out, `output_format` reported, 3-reference ceiling from
      xAI's API mirrored as a 400. Live with konaogco's cards: identity
      survives, style drifts photoreal; see README *Image generation (grok)*.
- [x] (grok) Image input in chat (2026-10-04): attachments to disk under
      `refs/<session>/`, seen via `read_file`, editable via `image_edit`;
      all tool calls gated by an ACP client hook (`_x.ai/hooks/run`) that
      denies paths outside the refs dir and grok's session store. The
      "images dropped" note is gone on grok.
- [x] (grok) `/v1/videos` (2026-10-05): OpenAI Videos API shape over
      `image_to_video`, synchronous, reference image mandatory, MP4 served
      on `/content`, size and duration read from the MP4 boxes. Needs the
      account's `/privacy` (ZDR) off or an S3 bucket. Chat stays video-free
      via the hook. `test_videos_api.py`.
- [ ] (grok) Still unverified: whether `grok agent` honours
      `[cli] auto_update = false` or needs it in `~/.grok/config.toml`, and
      what an expired 7-day token looks like on `session/new`.
- [x] **Persistent `claude` backend with `/clear` per task** (2026-09-23).
      Reuses one lean stream-JSON worker, consuming both reset acknowledgement
      and successful reset result before every subsequent task. Safe mode,
      no tools/MCP/saved sessions; normal subscription auth retained. Model
      changes restart; failed reset falls back to fresh workers. Cancellation,
      disconnect, early EOF and failed results retire/reap the worker.
      Offline lifecycle tests plus live Sonnet reset/isolation and Flask
      streaming checks passed on Claude Code 2.1.281. Timings are noisy but
      the repeated spawn is removed and input context fell substantially.
      See `archive/CLAUDE-STARTUP-2026-09-23.md` and
      `_bench/claude_startup_bench.py` for measurements and limitations.

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
    - ~~`qwen3-code`~~ — DECLINED 2026-07-28 (see TODONT.md): Qwen sells a
      direct OpenAI-compatible API to any subscriber, so there is no locked
      model for agentry to unlock. Not a deferral — off the list.
    - ~~`antigravity-cli`~~ — evaluated, shelved (see "Done").
    - ~~`codex` (OpenAI)~~ — DONE, landed as a backend.
  For any new candidate: implement the `Backend` ABC in `backends.py`,
  wire it into `make_backend`, add to the `--backend` choices.

## Polish (lower priority)

- [ ] Purge `.claude/memory/` from git HISTORY (sober-morning job). 435c53f
      untracked the session memories and gitignored the directory, but every
      version committed between 945a201 and 3bb161c is still readable at
      those revisions in the public repo. If any content genuinely shouldn't
      be public: `git filter-repo --path .claude/memory --invert-paths`
      (or BFG), then force-push. Solo repo, no other clones to coordinate —
      just re-clone/reset the other machines afterwards. If it's all
      brag-tier, close as won't-fix.

- [ ] (claude, LOW priority — claude-code stays a second-tier client until a
      better integration exists) Proper quota/cost for the claude backend by
      borrowing from `C:\devel\aweussom\python\claude-code-quota`
      (github.com/aweussom/claude-code-quota). Today agentry only *passively*
      reads that tool's `~/.claude/quota-data.json` cache, so the 5h/weekly
      numbers go stale unless an interactive claude-code session elsewhere
      keeps refreshing them (headless `claude -p` doesn't tick the status
      line). The fetch itself lives in the repo's `quota-lib.ps1` /
      `quota-lib.sh`: OAuth-token quota read + cache write with TTL (60s
      active / 5min idle), non-blocking background refresh, and stale/error
      marking. Port that refresh logic into `ClaudeCodeBackend.quota_status()`
      (Python, direct — not shelling out to the ps1/sh) so agentry keeps its
      own cache fresh; keep reading the shared cache file so the statusline
      and agentry don't double-fetch.

- [ ] Chat persistence in the web UI (from the OpenWebUI comparison,
      2026-07-24): F5 currently destroys the conversation. localStorage
      only — persist `chatHistory` client-side, restore on load. No
      server-side chat store; state ownership stays with the client.
- [ ] Stop + regenerate buttons in the web UI. Esc-to-cancel exists but
      is undiscoverable; add a visible stop during generation and a
      regenerate on the last assistant message (resend same user text).
- [ ] Surface usage stats in the UI's per-turn meta line, not just the
      launcher console. The copilot backend now accumulates per-turn AI
      credits (2026-08-13) — remaining work is riding the figure into the
      SSE stream (e.g. an extension field on the final chunk) and the UI
      meta line. codex equivalent: token counts from turn/completed.
- [x] ~~Add a model dropdown to the web UI~~ — done 2026-08-13, see the
      OpenAI-like model handling entry under Done.
- [ ] Probe whether `xhigh` / `max` reasoning levels work END-TO-END on
      the copilot SDK backend. models.list now advertises none..max on the
      gpt-5.6 models and the HTTP layer forwards them (2026-08-13), but a
      rejected level only logs a WARN and keeps the previous one — verify
      each level actually applies (get_current after set_model) before
      trusting bench numbers taken at xhigh/max.
- [x] ~~(codex) Yield reasoning deltas from the codex backend~~ — done
      2026-08-13, see the codex refresh entry under Done. Gotcha discovered:
      codex emits NO reasoning notifications unless thread/start passes
      `config: {model_reasoning_summary: ...}`.
- [ ] (codex) Switch to per-thread server-truth cost when codex-cli ships
      the `account/usage/read {threadId}` variant (documented in codex-rs
      main as returning estimated credits/cost per thread; 0.147.0 rejects
      params with "expected unit"). Replaces the local `_CREDITS_PER_MTOK`
      rate-card estimate, which silently drifts when OpenAI reprices.
- [ ] (codex) Consider `ephemeral: true` on thread/start so agentry's relay
      threads stop accumulating in codex's thread history / TUI resume list
      (ThreadStartParams supports it since ~0.147).
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
