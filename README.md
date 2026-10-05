# Agentry

**Point your OpenAI SDK at the coding-agent subscription you already pay for.**
*The agent built to call tools becomes the tool.*

Agentry wraps a coding-agent CLI — GitHub Copilot, OpenAI Codex, Claude
Code, or Grok Build — holds it as one persistent process, and serves the model behind it as
an OpenAI-compatible HTTP API on localhost. Your scripts and pipelines talk to
`gpt-5.6-luna` or `claude-sonnet` through the subscription you're already
logged into: no separate API bill, no per-call spawn tax (~8 s in `-p` mode
drops to the model's own ~1.5 s floor).

[![A manic developer in a Norwegian sweater smashing an acoustic guitar into a laptop, keyboard keys flying out of the soundhole. The whiteboard reads "DAGENS PLAN: 1. Fikse litt på søk ✓ 2. Legge til AI ✓ 3. En liten proxy ✓ 4. ??? 5. Profit (kanskje)"](./images/dev-to-article-header.png)](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de)

Prefer the unhinged origin story to sysadmin-grade docs? [The dev.to version
is here](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de).

A minimal chat **web UI** ships with the proxy — markdown with code-copy,
image attach, live collapsible thinking blocks, a model picker fed by
`/v1/models`, and an artifact side panel that renders fenced `html`/`svg`/
`markdown` blocks. On the `codex` and `grok` backends it also **generates
and edits images** through the CLI's built-in image tool (codex's
`gpt-image-2`, grok's `image_gen`/`image_edit`), billed to the subscription
rather than an API — inline in chat, and as OpenAI-shaped
`/v1/images/generations` and `/v1/images/edits` routes (see *API*). It is not the point of the project, just proof the API
works end-to-end. The launcher prints the URL (`http://localhost:8765`).

![Bundled chat UI talking to the proxy as a regular OpenAI endpoint: markdown answer with a copy button on the code block, a collapsible thinking block above it, a per-turn backend + latency tag, image attach, and a header showing the active model and reasoning effort](./images/web-ui.png)

> **Intended use:** a personal, localhost-only adapter. Each backend stays
> authenticated through its own official client and remains subject to that
> provider's terms — agentry adds no access path, credentials, or multi-user
> service on top. The `copilot` backend rides the official Copilot SDK, a
> supported product surface; `codex`, `claude` and `grok` wrap interactive
> CLIs programmatically and sit in the usual gray ToS zone — use a
> non-critical account there, keep volume modest, and never expose the port
> publicly.

## The idea: reverse MCP

MCP standardizes one direction: how a model *consumes* tools
(`LLM ──▶ tools`). Agentry points the arrow the other way. A coding-agent CLI
is an MCP *client* — it exists to call tools. Agentry confiscates them and
serves the bare model back out, so ordinary software consumes the model
instead (`code ──▶ LLM`).

Copilot sessions use an empty tool allowlist and a deny-all permission handler.
Claude disables built-in tools and MCP servers at launch. Codex refuses
client-side requests at the wire (JSON-RPC `-32601`) and uses
`approvalPolicy: never`, a read-only sandbox, an empty working directory and
chat-only instructions, with an explicit image-generation exception. Those
Codex settings do not establish that every runtime-internal tool is disabled.
The intended result is a language service behind an OpenAI-shaped API.

## Backends

Selected with `--backend`. Adding one means implementing a single `Backend`
class in `backends.py`.

| Backend | Wraps | Cost tier | Defaults |
|---|---|---|---|
| `copilot` (default) | the official [Copilot SDK](https://github.com/github/copilot-sdk) | Copilot AI credits per token (1 credit = $0.01); `gpt-5.6-luna` is the cheap band ($0.20/M in), ~10× under `gpt-5.6-terra`, ~20× under `gpt-5.6-sol` (live rate card 2026-09) | `gpt-5.6-luna` @ `low` |
| `codex` | `codex app-server` (persistent JSON-RPC stdio) | ChatGPT Go $8 / Plus $20; Codex credits per token, `luna` 25× cheaper than `sol` | codex's own configured model @ `low` |
| `claude` | persistent `claude -p` stream-JSON, conversation cleared per request | Claude subscription (premium) | `claude-sonnet-4-6` |
| `grok` | `grok agent stdio` ([Grok Build](https://github.com/xai-org/grok-build), Agent Client Protocol over stdio) | SuperGrok / X Premium+ subscription via `grok login`; no quota readout in the CLI | `grok-4.7` @ `high` |

All four backends reuse a runtime process. Claude clears its conversation
before every subsequent request, preserving independent tasks without paying
startup each time. Both the reset acknowledgement and its completion must
arrive; otherwise agentry falls back to fresh workers for the rest of that
backend's lifetime. Model changes restart the worker. Cancellation, timeouts,
failed turns and disconnected streams retire it so the next request starts clean.

Claude uses `--safe-mode`, `--strict-mcp-config`, `--tools ""`, and
`--no-session-persistence` to skip customizations, MCP, tools and saved
transcripts while retaining subscription authentication. Use a current Claude
Code CLI supporting these flags (validated on **2.1.281**). `--bare` is not
used because its authentication behavior differs. Startup measurements and
reproduction commands are in [the benchmark record](archive/CLAUDE-STARTUP-2026-09-23.md).

Grok opens one ACP session per chat (`session/new` is ~0.6 s). It switches
model and reasoning effort as session state before each turn.

The shipped agent profile `grok-agent-profile.md` removes its tools. `grok
agent` accepts none of the headless `--tools` / `--deny` flags, an empty
`tools:` list is ignored, and permission prompts never reach the client. So a
non-empty allowlist is the only thing that works. Validated on 1.0.46, [plan
and probes](archive/GROK-PLAN.md).

Three tools stay in: `image_gen` and `image_edit` (same carve-out as codex),
and `read_file` - which is how grok sees attachments.

The ACP prompt takes no image content. So an `image_url` attachment is written
under the scratch cwd's `refs/<session>/` and the message names the path.
grok's `read_file` returns it as an image block, so "what is in this picture"
works. A follow-up "now make the collar blue" hands the same path to
`image_edit`.

Every tool call passes a client hook agentry registers on `session/new`
(`_meta["x.ai/hooks"]`, reverse request `_x.ai/hooks/run`). `read_file` and
`image_edit` are denied for any path outside that refs dir or grok's own image
output dir. Any other tool is denied outright.

Grok fails OPEN if the hook reply is late or malformed, so the profile is the
first line and the hook is the second.

Grok also reads Claude Code's `~/.claude` skills and hooks by default; the
profile keeps them from mattering.

`XAI_API_KEY` is stripped from grok's environment so turns bill the
subscription, not the metered API.

Sessions and generated images persist under `~/.grok/sessions/` and are left
for the user to clean up. Attachment copies are removed when the chat ends.
## Quick start

Prerequisites: Python 3.11+ plus the CLI login for the backend you use:

- `copilot` — `copilot login` once from any installed Copilot CLI. The SDK
  downloads its own pinned native runtime (Node.js not needed to *run*
  agentry) and reads the same `~/.copilot` credential store. With a
  Microsoft Store Python the runtime lands in `.copilot-runtime/` inside the
  repo instead of `%LOCALAPPDATA%`, whose Store virtualization breaks the
  native loader.
- `codex` — Codex CLI on PATH, `codex login` (ChatGPT account, no API key).
- `claude` — Claude Code CLI on PATH and logged in; `-p` never prompts.
- `grok` — Grok Build CLI (`irm https://x.ai/cli/install.ps1 | iex` or
  `curl -fsSL https://x.ai/cli/install.sh | bash`), `grok login` with the
  grok.com account. `~/.grok/bin` is found even before PATH is refreshed.
  Leave `XAI_API_KEY` unset.

**Windows (PowerShell 7+)** — run from the same logon session as your
interactive `copilot login`, or the credential-store token is unreachable:

```powershell
.\start.ps1                          # copilot, gpt-5.6-luna, effort=low
.\start.ps1 -Backend codex           # model from codex config
.\start.ps1 -Backend claude
.\start.ps1 -Backend grok             # grok-4.7, effort=high
.\start.ps1 -Port 9000
.\start.ps1 -Model gpt-5.6-terra -ReasoningEffort high   # or --model/--reasoning-effort
```

`start.ps1` takes PowerShell flags (case-insensitive, prefixes like
`-Reasoning` work) and forwards GNU-style `--flags` to `agentry.py`
unchanged, so the `start.sh` spelling works on Windows too.

**Linux / WSL2** — log in inside your Linux environment (in WSL2: inside WSL,
not via the Windows host; npm is only needed for this login step):

```bash
npm install -g @github/copilot && copilot login     # one-time
./start.sh                           # flags: --backend codex|claude|grok, --port 9000
```

Open `http://localhost:8765` and chat. From WSL2 the same URL works in a
Windows browser via automatic port forwarding.

![Launcher console: the SDK client starts in under two seconds, reports the authenticated GitHub login, opens a session, and settles into the idle heartbeat — every subsequent chat request lands on the same warm process](./images/startup-console.png)

## Configuration

Launcher params (`start.ps1 -Flag` / `start.sh --flag`; `start.ps1` accepts
both spellings):

- **Port** — HTTP port, default `8765`.
- **Backend** — `copilot` (default), `codex`, `claude`, or `grok`.
- **Model** — override the default.
  - `copilot`: set as the SDK session's model, validated against your plan's
    `models.list`. The available set tracks Copilot's plans — check your
    model picker, not this README. Nothing is hardcoded: a model that
    appears in the Copilot CLI picker (e.g. `gpt-5.6-terra`) is usable
    immediately, per request or via `-Model`.
  - `codex`: sent per `turn/start`. When unset, each thread runs whatever
    `~/.codex/config.toml` says — and the codex TUI *writes your last picked
    model there*, so agentry silently follows TUI switches unless you pin
    `-Model`. Deliberate (it tracks OpenAI's model migrations for free), but
    pin for prod. The startup log's `codex thread: ... (default model=...)`
    line shows what each thread resolved to.
  - `claude`: passed to `claude --model`.
  - `grok`: `session/set_model` on the chat's ACP session. `grok-4.7`
    (default), `grok-4.6`, `grok-4.5`; the CLI also lists
    `grok-4.7-build-fast` ("2x the price"), which agentry hides.
  - A pinned model is validated at startup against the backend's model list
    (copilot `models.list`, codex `model/list`, grok's `initialize`
    model state); an unknown id exits with
    code 2 and prints what *is* available, instead of coming up "ready" and
    failing every turn. Codex's ids are OpenAI's (`gpt-5.6-sol` /
    `-terra` / `-luna`, `gpt-5.5` as of 2026-09) — there is no Claude on
    codex.
- **ReasoningEffort** —
  `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra`. What applies
  is per model: copilot's gpt-5.6 models advertise `none`→`max`, codex takes
  `none`→`ultra` (`ultra` is codex-only: "maximum reasoning with automatic
  task delegation"); a model that rejects a level keeps its previous one
  (WARN, not an error). grok takes `low`/`medium`/`high`/`xhigh` (`none` and
  `minimal` map to `low`, `max` and `ultra` to `xhigh`, and `grok-4.5` has no
  `xhigh` so it gets `high`); the launcher default is `high` there, `low`
  elsewhere. **No-op on `claude`** — agentry does not yet forward the CLI's
  `--effort` setting.

### Per-request model and effort

Launcher values are only defaults. Like a normal OpenAI endpoint, model and
effort are **request fields, not server state** — each turn runs on exactly
what its request asked for, applied atomically with the turn:

- `"model"` — copilot: the live session is switched under the turn lock
  (conversation history preserved); codex: a `turn/start` param; claude: the
  worker's `--model` (restarted when it changes); grok: `session/set_model`
  plus `session/set_config_option reasoning_effort` on the chat's session,
  inside the turn lock, only when they differ from what the session already
  runs. Ids are validated against the account's model list;
  unknown ids return an OpenAI-style `404 model_not_found` instead of
  silently running a fallback. Requests that omit `model` run the launcher
  default (or the backend's own default when unpinned) — selection is never
  sticky.
- `"reasoning_effort"` — same vocabulary as `-ReasoningEffort`, same
  per-turn semantics.

Concurrent clients can safely request different models: turns serialize
through one lock and each runs its own selection. On copilot they still
share one *conversation* (single session), so history is common even though
the model per turn is not. `/health` and the `active` flag in `/v1/models`
report the model a selection-less request runs on, verified against the
runtime where possible — a pin silently overridden by org policy shows up
there as the override, not the wish.

## Console & quota

Idle, the console pulses an in-place heartbeat; during a turn it becomes a
news ticker scrolling the model's current reasoning/output line. Each backend
also meters spend live:

- **copilot** — per-turn AI-credit cost from the SDK's usage events
  (`turn cost 0.011 credits (session total 1.455)`), plus this machine's
  calendar-month total read from the Copilot runtime's own ledger
  (`~/.copilot/session-store.db`). The account-wide plan meter counts every
  device and isn't in any public API, so the machine figure is a floor. The
  ready-line prints which login (`user=...`) is paying.
- **codex** — a quota line at idle and every ~10 min
  (`codex plus quota | weekly 99% left (resets 20 Aug 17:04)`), plus each
  turn's exact tokens and rate-card cost estimate
  (`tokens in=12677 (cached 9984) out=6  ~0.019 credits`). Primed at startup
  and kept fresh by codex's own push notifications — no extra API calls.
- **claude** — real 5-hour/weekly OAuth usage when
  [`claude-code-quota`](https://github.com/aweussom/claude-code-quota) is
  installed (agentry reads its cache passively); otherwise the coarse
  `rate_limit_event` claude streams per turn (status + reset, no %).

## API

- `GET /health` — readiness probe.
- `GET /v1/models` — the account's real model list (copilot `models.list`,
  codex `model/list`) with an `active` flag and credit `price_category`;
  single synthetic entry when the backend can't enumerate.
- `POST /v1/chat/completions` — SSE streaming in standard OpenAI delta
  format; images ride as `image_url` data: URIs; copilot's streamed reasoning
  summaries are forwarded as `delta.reasoning_content` (the
  DeepSeek-popularized extension; standard clients ignore it). Images the
  backend *generates* (codex, see below) arrive as `delta.images` /
  `message.images` — OpenRouter's convention, a list of `image_url` data:
  URIs — never inline in `content`, so a client parsing JSON out of the
  reply is unaffected.
- `POST /v1/images/generations` — OpenAI Images API shape over codex's
  (or grok's) built-in image tool: `{"prompt": ...}` → `{"data": [{"b64_json",
  "revised_prompt", "size"}], "output_format": "png"|"jpeg"}`. One image per call. `size` is honored as an
  **aspect ratio**, not a pixel count: codex pins `gpt-image-2` at
  `size=auto`, which reads the prompt, so agentry turns `1536x1024`,
  `1024x1536`, `1024x1024` or any `WxH` into explicit orientation wording,
  and the output lands at that ratio inside a fixed ~1.57-megapixel budget
  (1536×1024, 1024×1536, 1254², 1672×941 for 16:9). Each `data[]` entry
  carries the PNG's real `size`; resize downstream if you need exact
  pixels. `quality` is accepted and ignored (hardcoded `auto`). Other
  backends answer 501.
- `POST /v1/images/edits` — the same, anchored on reference images: OpenAI's
  multipart form (`image` / `image[]` file parts + `prompt`), or JSON with
  `image` as a data: URI (or a list of up to 5). The references ride the
  codex turn as attached images and its tool edits against them. Without
  `size`, the output takes the reference's proportions but the *prompt*
  decides orientation (a downstream 16:9 reference came back 941×1672 for
  "a standing figure filling the frame"); pass `size` to pin it. `mask` is
  rejected (400):
  codex's tool takes whole-image references only. Both Images routes run
  each call on a throwaway codex thread, so a batch of edits never
  accumulates old references in one context and never disturbs the chat
  session; they still serialize with chat turns through the one turn lock.
- `POST /v1/videos` — the OpenAI Videos API shape over grok's
  `image_to_video` (grok only, 501 elsewhere). `prompt` plus a mandatory
  `input_reference` (multipart file part, or JSON `{"image_url": "data:..."}`);
  grok has no text-only video. `seconds` (grok's floor is 6) and `size` are
  advisory like the Images routes. Generation is synchronous, ~45 s for a 6 s
  clip, so the returned video object is already `completed` (or `failed`
  with `error`), with the delivered `size` and `seconds` read from the MP4.
  `GET /v1/videos/{id}` returns the object, `GET /v1/videos/{id}/content`
  the MP4 (`variant=video` only), `GET /v1/videos` lists, `DELETE` forgets
  the id and leaves the file. Ids live in memory; the clips stay under
  `~/.grok/sessions/<cwd>/<session>/videos/`. Chat completions never carry
  video: the backend's hook denies the video tools outside a `/v1/videos`
  turn.
- `POST /v1/cancel` — cancels the in-flight turn (copilot `session.abort()`,
  codex `turn/interrupt`, grok `session/cancel`, claude kills the process).

### Image generation (codex)

codex-cli ≥ 0.149 ships image generation as a stable, on-by-default
built-in tool, and it works over `app-server` under agentry's locked-down
thread (no approvals, read-only sandbox, empty cwd — probed on 0.154.0,
`_bench/codex_imagegen_probe.py`). agentry's chat-only developer
instructions carve it out as the one permitted tool, *only when the user
explicitly asks for an image*, so enrichment prompts stay image-free. Ask
for a picture in the web UI and it renders under the reply; ~15–20 s per
image with a short prompt, ~45 s with a long prompt plus a 0.7 MB reference
(downstream measurement), calls serialize through the one turn lock, so a
batch of 30 is ~20 min of wall clock. ~0.7–1.4 MB PNG; a copy is also left in
`~/.codex/generated_images/<thread>/` by codex itself.

The time scales hard with input. A real comic pipeline on 2026-10-04 (prompt
1,000-4,800 chars, 4-5 reference images, `size` 1024x1536, `gpt-6-sol` @
medium) took 2.5-3.5 min per image. Three edits/generations measured 3 min 23
s, 2 min 40 s and 3 min 32 s from the client. 152-195 s of that was the single
`imageGeneration` item. One tool call per turn, near-empty reasoning, agentry
overhead ~5 s. Dropping from 5 to 4 references gained nothing measurable. Same
prompt and references against OpenAI's Images API with `gpt-image-2.5-flare`
took ~25 s. `/v1/images/edits` is a real edit, though. In both tests the strip
came back pixel-close with only the requested change (two deer added to one
panel, a pile of fur removed from another).
The chat model does not change the picture — every model on the account
(`luna`, `terra`, `sol`, `gpt-6-astra`, `gpt-5.5`) gets the same
`gpt-image-2` tool, honored the aspect, and took 40–58 s
(`_bench/codex_image_model_probe.py`). What differs is the **rewrite**: the
chat model rephrases your prompt before handing it to the image model.
`luna` forwards it almost verbatim; the bigger models triple it with
content you never asked for (`sol` added "no weapon required" and the
knight lost his sword). For a pipeline that wants *its* prompt to reach the
image model, the cheap default is the right choice. The tool is offered
only on a paid ChatGPT plan with ChatGPT login — a Free plan or an
`OPENAI_API_KEY` login does not get it (codex `spec_plan.rs`).

Cost is the point: the image bills against the **ChatGPT-plan window**
(three probe images moved a Plus 5-hour window by at most one integer
percentage point in total), not against API pricing, where the same
image on `gpt-image-*` is ~1 US cent. OpenAI's own guidance is that
image turns consume included usage "3–5× faster" than text turns — that is
a server-side weighting, not extra tokens: an image turn reports ~10
output tokens, so agentry's per-turn credits estimate understates it.
For batch generation OpenAI points at `OPENAI_API_KEY` billing instead.

### Image generation (grok)

Grok Build ships `image_gen` (plus `image_edit`, `image_to_video`,
`reference_to_video`) as built-in tools. The agent profile keeps `image_gen`
and `image_edit`, under the same "only when explicitly asked" clause. So a
chat request for a picture on the `grok` backend renders inline too: the tool
writes a JPEG (1024×1024 for a square prompt, ~75 KB) under
`~/.grok/sessions/<cwd>/<session>/images/` and reports the path. Agentry reads
it back into `delta.images` / `message.images` with the model's rewritten
prompt as `revised_prompt`. Measured 2026-10-04 on 1.0.46: ~6 s from tool
start to file, ~12-14 s for the whole turn with a short prompt. Grok's
per-turn cost estimate does not include the image.

`/v1/images/generations` and `/v1/images/edits` work on `grok` too. Each call
runs on a throwaway ACP session. References for edits are written to the
scratch cwd and handed to grok's `image_edit` tool as file paths (the ACP
prompt itself takes no image content), then deleted. Differences from codex,
all measured 2026-10-04 with a four-panel strip prompt of ~1.5k chars: output
is **JPEG** (the response's top-level `output_format` says so), 832×1248 for a
2:3 request, 20-30 s per call including the model's rewrite, and **at most 3
reference images** - xAI's API rejects more ("This model supports at most 3
input image(s)"), so agentry returns 400 above that, as it does above 5 on
codex.

Style note from the first real strips: `image_gen` from canon text alone
followed a "modern 3D cartoon" instruction well. `image_edit` with character
cards kept the identities but drifted toward photorealistic rendering and was
more erratic (a head on the wrong body, a character swapped for a deer), with
or without reinforced style wording. The comic pipeline that drives agentry's
edits route tried it the same day with combined character cards: style and
lettering right, identities recognisable, but roles and figures swapped
between panels, a dog changed breed, and a character was doubled. Verdict was
"not a candidate for strips today", at ~1.2 cents a call.

Judge for your own material.

Video, too. Grok Build ships `image_to_video` and `reference_to_video`, and
both need an input image (`reference_to_video` without one fails with
"Provide at least one input: `images` (up to 14), `voices` (up to 3),
`first_frame`, `last_frame`, and/or `keyframes` (up to 4)"). They also need
the account's `/privacy` (zero data retention) setting off, or a
user-hosted S3 bucket in `managed_config.toml`; under ZDR the tools return
an error. Measured 2026-10-05 on 1.0.46 with a character card as reference:
6 s clip (the tool's floor), 448x672 at the model's default 480p in 29 s,
768x1168 at 720p in 39 s, MP4 H.264 + AAC, 1.1 to 3.2 MB. The cost estimate
grok reports does not include the clip. Exposed as `/v1/videos` (see
*API*), not in chat.

## Architecture

`agentry.py` is the Flask layer (routes, OpenAI shape, session reuse);
`backends.py` holds the `Backend` ABC and the four implementations. Flask
talks only to the interface (`new_session` / `prompt` / `cancel` /
`is_alive` / `close`), so swapping backends is a flag; per-request model and
effort ride as `prompt()` arguments, applied inside each backend's turn lock.

The turn lifecycle is hardened against the ugly paths: a codex `turn/start`
error (bad model, dead thread) surfaces to the client immediately instead of
stalling to the stream timeout; a turn abandoned by timeout is cancelled
server-side so it stops burning quota; and stray updates from an abandoned
turn are filtered by turn id, so a zombie turn can never bleed text into the
next request's response.

`.github/copilot-instructions.md` pins per-session behavior for the copilot
backend (treat prompts as standalone chat, no repo reads, no tool requests,
be terse), overriding any global `~/.copilot/` instructions that would leak
context hints into prompts.

## File map

| Path | Purpose |
|---|---|
| `agentry.py` | Flask server + OpenAI surface + backend selection |
| `backends.py` | `Backend` ABC + copilot / codex / claude / grok implementations |
| `logutil.py` | Timestamped logging + idle heartbeat/ticker |
| `templates/`, `static/` | Web UI |
| `.github/copilot-instructions.md` | Per-project chat-only instructions (copilot) |
| `grok-agent-profile.md` | Agent profile that strips grok's tools down to `image_gen` and sets its chat-only prompt |
| `test_*.py` | Offline regression tests: HTTP selection race, CLI model pin, Claude lifecycle, Grok ACP lifecycle, Videos API |
| `start.ps1` / `start.sh` | Launchers (create venv, run agentry) |
| `TODO.md` / `TODONT.md` | Roadmap / paths intentionally not taken |
| `archive/` | Backend design + validation records |
| `logs/` | Runtime wire traces (gitignored) |

## Known limits

- **Tool requests are always denied** — by design, with one carve-out:
  codex's and grok's built-in image generation, and only when a message
  explicitly asks for an image (see *Image generation*); on grok also
  `read_file`, gated to the attachment folder, because that is its only way
  to see an image. Everything else — shell, other file reads, MCP — is
  refused, so a prompt that genuinely needs a tool degrades or errors rather
  than working around it.
- **Reasoning trace depends on backend.** Copilot's and codex's streamed
  summaries reach the console ticker and the web UI think-block, grok's
  thought chunks reach the web UI; claude forwards none.
- **Auth is inherited, not configured.** There is no token setting: agentry
  uses whatever login the backend CLI already has for the user running it
  (`copilot login`, `codex login`, `grok login`, Claude Code's own login).
  Grok's token expires after 7 days without a refresh; a failing
  `session/new` then surfaces as an error telling you to log in again. Start it from
  that user's own shell. Running it as a Windows service or under another
  account will not find the Copilot credential, which lives in the
  per-logon credential store.
- **Single user, single session.** Concurrent clients share one backend
  session and serialize through one turn lock — personal use, not
  multi-tenant.
- **Codex carries a fixed ~24.8k-token harness per turn** (its agent system
  prompt + tool schemas). Not reducible, cached server-side, not separately
  billed on a flat subscription. Beyond cost, that prefix is agent
  scaffolding the model reads before your prompt, so it can colour
  responses. Copilot is not free of this either, just ~5x lighter: the
  runtime's ledger shows a 22-byte prompt billed at ~5.3k input tokens on
  `gpt-5.6-luna` (~2.9k on `gpt-5-mini` in July), and about 900 of those
  are agentry's own `.github/copilot-instructions.md`, which is the one
  lever we do hold — it asks for terse, chat-only replies. Grok's default
  harness is ~16.6k tokens (tool schemas plus every Claude Code skill it
  finds under `~/.claude`); the agent profile cuts it to ~5.6k.

## Related work

The same itch — "I already pay for this agent, let my own code call the
model" — has been scratched once per vendor, many times over. Ordered by
maintenance activity as of 2026-09-11 (most recently pushed first); stars
are from the same day and will drift.

| Project | Wraps | How | Status 2026-09-11 |
|---|---|---|---|
| [`caozhiyuan/copilot-api`](https://github.com/caozhiyuan/copilot-api) | Copilot, codex, third-party APIs | reverse-engineered HTTP; Chat Completions + Responses + Anthropic Messages; Electron desktop app | very active — releases several times a week; ~1k★ |
| [`icebear0828/codex-proxy`](https://github.com/icebear0828/codex-proxy) | codex | ChatGPT backend API with the OAuth token; OpenAI/Anthropic/Gemini protocols; non-commercial license | very active; ~1.7k★ |
| [`messense/copilot-api-proxy`](https://github.com/messense/copilot-api-proxy) | Copilot | Rust reverse proxy, OpenAI + Anthropic endpoints | active; small |
| [`hotchpotch/openai-api-server-via-codex`](https://github.com/hotchpotch/openai-api-server-via-codex) | codex | Go server on the codex login token; PyPI/binaries | active; ~50★ |
| [`wende/claude-max-api-proxy`](https://github.com/wende/claude-max-api-proxy) | Claude Code | spawns `claude -p` per request; OpenClaw integration | active; ~130★ |
| [`theblixguy/copilot-sdk-proxy`](https://github.com/theblixguy/copilot-sdk-proxy) | Copilot | the **official Copilot SDK** (TypeScript); Chat Completions + Anthropic + Responses; npm; the core of [`xcode-copilot-server`](https://github.com/theblixguy/xcode-copilot-server) | dependabot-only since 2026-07; ~10★ |
| [`rezrov/copilot-proxy`](https://github.com/rezrov/copilot-proxy) | Copilot | the official Copilot SDK (Node); client-owned tool loop; proposed in [copilot-sdk discussion #218](https://github.com/github/copilot-sdk/discussions/218), unanswered by GitHub staff | quiet since 2026-07; ~10★ |
| [`vkop007/codex-app-proxy`](https://github.com/vkop007/codex-app-proxy) | codex | persistent `codex app-server` — the same surface agentry's `codex` backend uses | abandoned 2026-02 |
| [`anshulpatel25/copilot-sdk-gateway`](https://github.com/anshulpatel25/copilot-sdk-gateway) | Copilot | Python Copilot SDK, one client per request | archived 2026-05 (author cites the move to usage-based billing) |
| [`ericc-ch/copilot-api`](https://github.com/ericc-ch/copilot-api) | Copilot | reverse-engineered HTTP; the original | dormant since 2025-11, 130+ open issues; ~4k★ — use the caozhiyuan fork |

Where agentry sits: every project above wraps **one** vendor; agentry puts
Copilot, codex, Claude Code and Grok Build behind the same endpoint, and
drives each through its supported surface (Copilot SDK, `codex app-server`,
`claude -p`, `grok agent stdio`) rather than reverse-engineered HTTP. It is also the only one that
**strips the tools** — the others expose tool execution as a feature, agentry
serves the bare model (see *reverse MCP* above) — and the only one with a
live credit/quota line in the console, which matters once a batch can drain
a month's allowance overnight. Want a broad multi-client gateway with a GUI?
Pick caozhiyuan or icebear0828. Want a thin local wrapper over the official
surfaces, with backend choice per launch? That's agentry.

## Acknowledgments

- [Agent Client Protocol](https://agentclientprotocol.com) by Zed Industries —
  the copilot backend's original transport, since replaced by the Copilot SDK.
- Web UI based on [NoLlama](https://github.com/aweussom/NoLlama) (an
  OpenVINO-based LLM server for Intel NPU/GPU).
