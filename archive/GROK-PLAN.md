# Grok Build backend plan

**Status (2026-10-04, later the same day): LANDED as backend #4.**
`GrokACPBackend` in `backends.py`, `--backend grok`, both launchers,
`grok-agent-profile.md`, `test_grok_backend.py` (15 offline tests). Live
smoke on a test port: models list hides the fast variant, streamed pong,
shell request answered `NO-TOOLS`, per-request switch to `grok-4.5 @ high`,
inline blue circle (74 KB JPEG, 12 s turn), image input dropped with the
note. Decisions taken: sessions and images stay on disk (user cleans up),
default effort `high`, `grok-4.7-build-fast` hidden. The probe record below
is as written before landing.

**Phase 2 (same afternoon): Images routes on grok.** `image_edit` takes
`{"prompt", "image": [absolute paths]}`; `GrokACPBackend.image_turn()` writes
the client's references under `<scratch cwd>/refs/<uuid>/`, names them in the
instruction, runs a throwaway session and removes the files afterwards.
Measured with the Kona & Co material (1.5k-char canon + four-panel draft,
`size` 1024x1536): generations 22 s, edits with 3 references 21–28 s, all
832×1248 JPEG. Six references failed inside the tool: `HTTP 400 ...
"This model supports at most 3 input image(s), but 6 were provided"`, now a
400 from agentry. `search_tool`/`use_tool` can be removed with
`disallowedTools`, so the profile does. Style: canon-only `image_gen` gave a
clean 3D-cartoon strip with correct Norwegian lettering but the wrong dog
breed; `image_edit` with cards kept Tommy, Kona and the Rottweiler but
rendered photoreal-ish and misbehaved (character/dog hybrid, a deer in
Missy's panel), reinforced style wording did not fix it. Samples in
`C:\temp\grok-agentry-samples\`.

**Phase 3 (same day): image input in chat.** Vision works through grok's own
`read_file`: its tool result for a JPEG is a `{"type": "image", "data":
<b64>}` content block, and the model described the Missy card correctly.
Letting `read_file` in needed a read boundary that applies in ACP mode. Found
in the source (xai-org/grok-build, `extensions/hooks.rs`,
`session/acp_session/hooks.rs`): the client registers hooks in `session/new`
`_meta["x.ai/hooks"] = {"PreToolUse": [{"matcher": "*", "hookCallbackIds":
[...]}]}` and grok sends the reverse request `_x.ai/hooks/run` with
`toolName`/`toolInput`; `{"decision": "deny", "systemMessage": ...}` aborts
the call ("Hook denied: ..."), `{}` lets it run. Verified: allowed read
returned the image, `C:/Windows/win.ini` was denied. Caveats from the gate
code: transport error, timeout and malformed replies all **fail open**;
`"ask"` is not supported. Not usable instead: project `.claude/settings.json`
deny rules (not loaded, `projectTrusted: false`, 0 sources), `--deny` (root
flag only), `support_permission` (no effect). ACP sessions run in
`permissionMode: "auto"`, which is why no permission request ever arrives.
Live through agentry: attach card → described in 7.5 s; "make the collar
blue" in the same chat → `image_edit` on the attachment, 21 s, sample 08;
"read win.ini" → refused by the model before the hook was needed.

**Phase 4 (2026-10-05): video.** Tools `image_to_video` `{image, prompt,
duration, resolution_name}` and `reference_to_video` `{prompt, first_frame,
aspect_ratio, duration, resolution_name}`; the latter without any image
fails with "Provide at least one input: `images` (up to 14), `voices` (up to
3), `first_frame`, `last_frame`, and/or `keyframes` (up to 4)", so there is
no text-only video. First attempt failed under the account's ZDR/`/privacy`
setting ("Video generation tools are unavailable under zero data retention";
alternatives: `/privacy` off, or `[tools.zdr_video_output_s3]` in
`managed_config.toml`, docs.x.ai/build/settings/zdr-video-storage). With
privacy off: 6 s floor, 480p default 448x672 in 29 s, 720p 768x1168 in
39 s, MP4 H.264+AAC, result reported like images as `{path, filename,
session_folder}` under `.../<session>/videos/1.mp4`. Landed as `/v1/videos`
(OpenAI Videos shape, synchronous) with `video_turn()` and a hook rule that
allows the video tools only on the video session. Samples 09 and 10 in
`C:\temp\grok-agentry-samples\`.

Downstream verdict (konaogco, same day, combined cards within the 3-ref
ceiling, 13 s image, ~1.2 cents): modern-cartoon style and lettering right,
identities recognisable, but the panel-by-panel draft was followed only
halfway: a shedding Rottweiler became a white Old English Sheepdog, Tommy
changed sides, Kona ended up rolling in the mud with dog paws, two Konas in
panel 4. Rejected for strips; the `[grok-agentry]` provider stays in their
ini for a retry when the xAI model or this route changes.

**Status (2026-10-04, morning): probes done, all gates cleared, nothing landed yet.**
`grok 1.0.46 (2765805b9442) [stable]` installed natively on Windows at
`C:\Users\tommyl\.grok\bin\grok.exe` via `irm https://x.ai/cli/install.ps1 | iex`
(the README calls Windows "best-effort, untested from this tree"; everything
below ran fine on it, so no WSL detour is needed). Auth: `grok models` reports
"You are logged in with grok.com", token in `~/.grok/auth.json`, 7-day expiry
with background refresh. `XAI_API_KEY` is **not** set and must stay unset in
the agentry process: it takes precedence and bills the metered API instead of
the subscription.

The TODONT bar for a new backend was "a clean persistent JSON/streaming
protocol AND a distinct audience". Both hold: `grok agent stdio` is real ACP
(the protocol the retired `CopilotACPBackend` spoke), and the audience is
SuperGrok / X Premium+ subscribers.

Probe: `_bench/grok_acp_probe.py` (ACP, logs every frame; `GROK_ARGS`,
`MODEL`, `EFFORT`, `VERBOSE` env knobs). The headless and streaming/cancel
checks below were one-off scripts in the session scratchpad; their numbers are
recorded here.

## Results (2026-10-04)

### Models and effort

From `grok models` and the ACP `initialize` result (`_meta.modelState`):

| Model | Notes | Efforts |
|---|---|---|
| `grok-4.7` (default) | "latest frontier model", 256k ctx (500k listed) | xhigh, high (default), medium, low |
| `grok-4.7-build-fast` | "Fast variant. 2x the price." | same |
| `grok-4.6` | | same |
| `grok-4.5` | | high, medium, low (no xhigh) |

Effort is a per-session ACP config option (`configOptions[].id ==
"reasoning_effort"`), set with the standard `session/set_config_option`
`{sessionId, configId: "reasoning_effort", value}`. Model is set with
`session/set_model {sessionId, modelId}`. Both answered instantly and emitted
a `model_changed` notification. Mapping agentry's vocabulary: none/minimal →
low, ultra → xhigh, xhigh → high on 4.5.

### ACP surface (`grok agent ... stdio`)

| Step | Measured |
|---|---|
| `initialize` | 0.15 s |
| `session/new` (empty scratch cwd) | 0.6 s |
| pong turn, high effort | 2.0 s, 16.6k input tokens (default profile) |
| 3 paragraphs, low effort, chat profile | thought first chunk 2.2 s, text first chunk 6.2 s, done 11.3 s |
| text streaming | 249 `agent_message_chunk`s over 5 s, 115 distinct timestamps: live, not end-flushed |
| reasoning | `agent_thought_chunk`s stream the same way |
| `session/cancel` mid-stream | prompt result `stopReason: "cancelled"` 10 ms later, no chunks after; same session answered the next prompt |
| `session/close` | `x.ai/closeOutcome: closed` |

Wire shape: standard ACP (`session/update` with `agent_message_chunk`,
`agent_thought_chunk`, `tool_call`, `tool_call_update`, `config_option_update`,
`available_commands_update`, `session_info_update`) plus a lot of `_x.ai/*`
housekeeping notifications (`models/update`, `announcements/update`,
`settings/update`, `session/setup`, `queue/changed`, `sessions/changed`,
`session_notification` with `response_completed` / `turn_completed` carrying
usage and `costUsdTicks`). Filter by `sessionId`; ignore the rest.

`promptCapabilities.image: false` → **no image input over ACP**. Drop with a
visible warning, as the Claude backend does.

No agent→client request (`session/request_permission`, `fs/*`) was ever
received, even for a shell command. Reply `-32601` to any that show up.

### Tool restriction: what works and what does not

Grok Build keeps tools in headless and ACP mode, and runs `echo` without
asking in both. These were tried against the prompt "Run the shell command
`echo agentry-probe` ... if you cannot, say NO-TOOLS":

| Mechanism | Mode | Result |
|---|---|---|
| `--tools ""` | headless | ignored (empty = no filter); command ran |
| `--disallowed-tools run_terminal_command` | headless | ignored; command ran (event name ≠ tool id) |
| `--disallowed-tools run_terminal_cmd` (README id) | headless | tool removed from the list |
| `--tools read_file` | headless | tools = read_file + MCP stubs; NO-TOOLS; 6.6k input tokens |
| `--tools todo_write --disallowed-tools todo_write` | headless | zero built-in tools, only inert `search_tool`/`use_tool`; 5.9k tokens |
| `--deny "Bash(*)"` | headless | blocked: "Denied by permission policy: deny rule on bash" |
| `--permission-mode plan` | headless | **did not block**; command ran |
| `[features] support_permission = true` in a dedicated `GROK_HOME` | ACP | **did not block**, no permission request sent |
| agent profile `tools: []` | ACP | ignored (empty list); command ran |
| agent profile `tools: [todo_write]` + `disallowedTools: [todo_write, Agent]` | ACP | **zero tool calls, NO-TOOLS, 5.6k input tokens** |

`--tools`, `--disallowed-tools`, `--deny`, `--permission-mode`, `--cwd`,
`--no-auto-update` and friends are root/headless flags. `grok agent` accepts
only `-m`, `--reasoning-effort`, `--always-approve`, `--agent-profile <PATH>`,
`--plugin-dir`, leader options; `grok agent stdio` accepts nothing relevant.
The flag order matters: `grok agent --agent-profile p.md stdio`.

**Decision: the agent profile is the read-only enforcement for ACP.** A `.md`
with YAML frontmatter; the body becomes the system prompt (input tokens fell
from 16.6k to 5.6k, so it replaces most of the harness prompt). Shipped
profile, to live next to `.github/copilot-instructions.md` as runtime prompt
content:

```markdown
---
name: agentry-chat
description: Chat-only profile for the agentry proxy
tools:
  - todo_write
disallowedTools:
  - todo_write
  - Agent
---
You are a helpful chat assistant answering through an API proxy. You have no
tools, no filesystem and no shell. Answer directly in the user's language.
```

The `search_tool`/`use_tool` stubs that survive are MCP discovery; with
`mcpServers: []` on `session/new` they have nothing to find. Belt and braces
still apply: empty scratch cwd, `-32601` to client requests.

### Claude Code compatibility is on by default

`grok inspect --json` from this repo showed grok loading
`~/.claude/Claude.md` as global instructions, every skill under
`~/.claude/skills/` (incl. synced ones), and the `pre_tool_use` hook from
`~/.claude/settings.json`. In the ACP probe that hook (detune's `gate.py`)
**actually ran inside grok** before the shell command (400 ms). With zero
tools, `pre_tool_use` never fires, so the profile defuses this too. The skill
descriptions still ride along in `available_commands`; whether they cost
prompt tokens with the profile active is not measured (5.6k total, so small).

`GROK_HOME=<dir>` (documented) works with a copied `auth.json` and its own
`config.toml` (`[skills] ignore`, `[subagents] enabled=false`, telemetry off):
the detune skill disappeared. But it moves `auth.json`, so the agentry copy
and the TUI copy would refresh the 7-day token independently. Not worth it
while the profile does the job. Keep as a fallback if the compat loading ever
leaks into answers.

### Images

Grok's tool list includes `image_gen`, `image_edit`, `image_to_video`,
`reference_to_video`. With a profile whose `tools` is `[image_gen]`, "Generate
an image of a plain red square on a white background" produced:

- `tool_call` with `rawInput {prompt: <model's rewrite>, aspect_ratio: "1:1"}`
- `tool_call_update status: completed`, content text = JSON
  `{path, filename: "1.jpg", session_folder: "images", message}`
- file `~/.grok/sessions/<urlencoded cwd>/<sessionId>/images/1.jpg`,
  1024×1024 JPEG, 82 KB, ~6 s from tool start to completed
- turn `costUsdTicks` 100 225 200 ≈ $0.010, i.e. the image itself is not in
  the reported cost (same caveat as codex credits)

So grok is a second image-generating backend. The chat path maps directly onto
the existing contract: read the file, yield
`("image", "image/jpeg", b64, rawInput.prompt)`. `aspect_ratio` is the
`size` hook for `/v1/images/generations`. Edits would need `image_edit` with
on-disk references written into the session cwd, since ACP takes no image
input; phase 2.

### Headless `-p` surface (not chosen, recorded)

Works on Windows, ~3.1 s wall for a pong including process start. Three
formats: `streaming-json` (ACP-native events `text`/`thought`/`tool_call`/
`usage`/`end`), `streaming-messages-json` (Anthropic Messages wire format;
with `--include-partial-messages` it streamed 207 `content_block_delta`s),
`json`. Sessions via `-s <id>`. It is the Claude-shaped fallback if ACP ever
breaks; no reason to prefer it while ACP is persistent and streams.

### Usage, quota, persistence

- Every turn reports `usage` with input/output/reasoning/cached tokens and
  `costUsdTicks` (1 tick = 1e-10 USD; 112 438 000 ticks = $0.0112). `grok
  usage <sessionId>` prints persisted per-turn totals.
- No subscription quota readout exists in the CLI or README (grep for quota,
  rate limit, credits, subscription: nothing). `quota_status()` can only show
  **this-run** cost estimate from the ticks, labelled as excluding images.
- Sessions persist in all modes under `~/.grok/sessions/<urlencoded cwd>/<id>/`
  (incl. generated images). No off switch found. `grok sessions delete <id>`
  exists; `grok sessions list` is cwd-scoped. Use **one stable scratch cwd**
  so every agentry session lands in one folder.

## Protocol mapping

| agentry `Backend` | Grok ACP |
|---|---|
| spawn | `grok agent --agent-profile <profile> stdio`, cwd = stable empty scratch dir |
| init | `initialize {protocolVersion: 1, clientCapabilities: {fs: {readTextFile: false, writeTextFile: false}, terminal: false}}`; `list_models()` from `_meta.modelState.availableModels` |
| `new_session(model, effort)` | `session/new {cwd, mcpServers: []}` → `session/set_model`, `session/set_config_option reasoning_effort` |
| `prompt(text, images, model, effort)` | under the turn lock: set model/effort if they differ from the session's; `session/prompt {sessionId, prompt: [{type: text, text}]}`; drop images with a yielded warning |
| text deltas | `session/update` → `agent_message_chunk` with `content.type == text` |
| `("reasoning", t)` | `agent_thought_chunk` |
| `("image", ...)` | `tool_call_update status: completed` for a `tool_call` titled `image_gen`: parse the JSON text, read `path`, sniff mime, b64 |
| turn end | the `session/prompt` response (`stopReason: end_turn / cancelled`) |
| `cancel()` | notification `session/cancel {sessionId}` |
| agent→client requests | reply `-32601` |
| `close()` | `session/close`, close stdin, terminate |
| `quota_status()` | running sum of `costUsdTicks` from `turn_completed` |

Reader-loop/request plumbing: copy the shape of `CodexAppServerBackend`
(`_reader_loop`, `_request`, `_notify`, pending-id map, one output queue per
turn owned by the request). The retired `CopilotACPBackend` (git `7bb24d2^`)
is the same protocol and still a fine reference for `session/update` parsing.

## Verification steps (in order)

1. `GrokACPBackend` in `backends.py`; `--backend grok` in `agentry.py`;
   `grok` in both launchers (PATH, then `~/.grok/bin/grok(.exe)`). Default
   model `grok-4.7`, default effort `low` (high is the CLI default and slow
   for chat).
2. Regression test with a fake process, like `test_claude_backend.py`:
   session per chat, model/effort switch inside the lock, cancel retires the
   turn not the process, image tool_call → image tuple, `-32601` on requests,
   image input dropped loudly.
3. Live: UI chat with thinking block, model picker fed by `list_models()`,
   cancel mid-stream, "draw me a red square" inline image, two concurrent
   requests on different models.
4. README backend table + AGENTS.md constraints paragraph for grok.
5. Phase 2: `/v1/images/generations` provider selection (codex|grok),
   `image_edit` with on-disk references.

## Open questions

- Session litter policy: leave `~/.grok/sessions` alone (consistent with the
  codex rule "do not delete generated images or session history") or
  `session/close` + `grok sessions delete` once the response is consumed?
- Token expiry: after 7 days without refresh `session/new` will presumably
  fail with an auth error. Surface it as a structured error telling the user
  to run `grok login`; untestable today.
- Whether `grok agent ... stdio` checks for updates at start (the root
  `--no-auto-update` flag is not accepted there; `[cli] auto_update = false`
  in `~/.grok/config.toml` is the documented alternative). Nothing slow was
  observed in the probes.
- Expose `grok-4.7-build-fast`? It is listed as 2× price; on a flat
  subscription that likely means 2× quota burn.

## Files referenced

- `_bench/grok_acp_probe.py` — ACP probe used for everything above
- `backends.py` — `CodexAppServerBackend` plumbing to copy; `ClaudeCodeBackend` image-drop wording
- git `7bb24d2^:backends.py` — retired `CopilotACPBackend`, same protocol
- `~/.grok/README.md` (111 KB, bundled with 1.0.46) — the real CLI reference; docs.x.ai `/build/cli/reference` and `/build/cli/headless-scripting` are thinner copies
