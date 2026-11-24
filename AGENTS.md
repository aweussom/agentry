# Repository Guidelines

## Project Structure & Module Organization

Agentry is a personal, OpenAI-shaped Flask proxy for coding-agent
subscriptions. It intentionally serves both localhost and the office LAN
(including an always-on laptop). Keep the default bind address `0.0.0.0`
and the absence of API authentication: these are explicit project decisions,
not omissions to fix during unrelated work. This is a single-user relay,
not a multi-tenant gateway.

`agentry.py` is the HTTP server and CLI entry point.
`backends.py` contains the Copilot SDK, Codex app-server, Claude Code,
Grok Build (ACP) and Antigravity `agy acp` (ACP) implementations; shared
console and request logging lives in `logutil.py`. `grok-agent-profile.md`
is runtime prompt content for the Grok backend, like
`.github/copilot-instructions.md` is for Copilot. `agy_hook.py` is the agy
backend's PreToolUse lockdown hook, run by agy itself.
The browser UI uses `templates/index.html`, `static/css/style.css`, and
`static/js/app.js`. Launchers are `start.ps1` for Windows and `start.sh` for
Linux/WSL. Keep exploratory scripts in `_bench/`; project notes and historical
plans belong in `TODO.md`, `TODONT.md`, and `archive/`.

`archive/AUDIT-2026-09-23.md` records the code audit, reproduced defects,
and validation limits. It is a dated baseline, not proof that later code
still has those defects. No `CLAUDE.md` currently exists in this repository.

The instructions in `.github/copilot-instructions.md` govern chat responses
served by the proxy. They are runtime prompt content, not development
instructions for agents maintaining this repository.

## Build, Test, and Development Commands

Use Python 3.11+ and the launchers, which create/manage `venv/`:

```powershell
.\start.ps1                         # Copilot backend on port 8765
.\start.ps1 -Backend codex -Port 9000
.\venv\Scripts\python.exe -m py_compile agentry.py backends.py logutil.py
.\venv\Scripts\python.exe test_agentry_race.py
.\venv\Scripts\python.exe test_session_reset_race.py
.\venv\Scripts\python.exe -m unittest -v test_claude_backend test_grok_backend test_agy_backend test_videos_api test_cli_model test_shutdown
```

```bash
./start.sh --backend claude --port 9000
venv/bin/python -m py_compile agentry.py backends.py logutil.py
venv/bin/python test_agentry_race.py
venv/bin/python test_session_reset_race.py
venv/bin/python -m unittest -v test_claude_backend test_grok_backend test_agy_backend test_videos_api test_cli_model test_shutdown
```

`test_agentry_race.py` checks concurrent request model/effort isolation,
unknown-model rejection, and default selection using a stub backend; it
requires no CLI authentication. `test_session_reset_race.py` checks audit
A02 with a stub that copies the Copilot backend's locking: concurrent new
chats each get their own session and a 200, and the chat lock comes back on
every exit path (stream closed unread or mid-read, failed `new_session`).
Live backend checks require the corresponding
CLI login and may consume subscription quota. Run relevant `_bench/` probes
only for the behavior being changed. Use a separate port for test instances
when an existing instance is running. Manually check UI changes in the browser.

`smoke_live.py` is the live drift check: a real agentry per backend on a
free localhost port, short turns, cancel, chat isolation and a canary file
read that must be refused (informational on codex). It uses subscription
quota and needs every CLI logged in. A daily Task Scheduler entry
(`agentry-smoke`) runs it with `--update --if-changed`; results are in
`logs/smoke/`. Run it after changing a backend's transport, and treat a
pass there, not the pinned versions above, as the current validation.

There is no frontend build step, configured linter, or CI workflow. With Node
and Bash installed, `node --check static/js/app.js` and `bash -n start.sh`
check syntax only. Use the PowerShell parser for `start.ps1` syntax checks;
executing the launcher can install dependencies, recreate an old venv, and
start a live backend. On Windows, prefer the documented PowerShell parameters
until the GNU argument/default interaction in the audit is fixed. An existing
Linux venv does not trigger dependency repair in `start.sh`.

The HTTP test covers selection with a stub, not actual SDK model switches,
Copilot/Codex session races, UI rendering, or Images API behavior.
`test_claude_backend.py` covers worker reuse, confirmed resets, model changes,
fallback, cancellation, disconnect and failure cleanup with fake processes.
`test_grok_backend.py` covers the ACP session lifecycle, in-turn model and
effort selection, effort mapping, image tool results, refused client
requests, cancel, timeout and disconnect with a fake `grok agent stdio`.
Add targeted regression coverage when fixing other paths. Most
`_bench/` scripts call live providers; some use retired ACP/protocol snapshots.
Inspect their entry points before running them, and do not bulk-execute them
as an offline test suite.

## Coding Style & Behavioral Constraints

Follow existing Python style: four-space indentation, `snake_case` for
functions and variables, and `PascalCase` for classes. Prefer standard-library
modules before third-party imports. Keep plain JavaScript and CSS consistent
with the existing UI; avoid introducing a framework without a clear need.

Keep backend transports separate behind the `Backend` interface. All five
backends reuse runtime processes, with different conversation lifecycles.

- Copilot bridges an asyncio loop thread to synchronous Flask workers with
  queues. Sessions disable tools and deny permission requests. Images use
  temporary file attachments, cleaned up after the turn. Keep the Microsoft
  Store Python runtime relocation and respect `COPILOT_CLI_EXTRACT_DIR`.
- Codex uses JSON-RPC stdio, an empty scratch cwd, chat-only developer
  instructions, `approvalPolicy=never`, and a read-only sandbox. Client RPC
  requests are rejected. These measures do not prove all built-in tools or
  filesystem reads are disabled; an empty cwd is not a filesystem boundary.
  Built-in image generation is an intentional exception for image requests.
- Claude uses a persistent `claude -p` stream-JSON worker with safe mode,
  no tools/MCP/session persistence, and a scratch cwd (validated on 2.1.281).
  Every subsequent prompt must consume both `/clear`'s `conversation_reset`
  and successful terminal result before sending user text, inside the turn
  lock. If reset fails, use fresh processes for the rest of the backend's
  lifetime. Model changes restart; cancellation, disconnect, timeout and
  failed turns retire the worker. Each worker owns a separate output queue.
  It still drops image input with a visible warning and does not forward
  effort. Do not use `--bare`: it changes subscription authentication.
  See `archive/CLAUDE-STARTUP-2026-09-23.md` for live validation and timings.
- Grok uses one persistent `grok agent --agent-profile grok-agent-profile.md
  stdio` process speaking ACP, one `session/new` per chat, and sets model
  and effort as session state inside the turn lock before `session/prompt`.
  The agent profile is the tool restriction: `grok agent` takes none of the
  headless `--tools`/`--deny`/`--permission-mode` flags, an empty `tools:`
  list is ignored, `--permission-mode plan` and `support_permission = true`
  do not block, and no `session/request_permission` ever reaches the client
  (validated on 1.0.46, `archive/GROK-PLAN.md`). Keep the allowlist
  non-empty; `image_gen`, `image_edit` and `read_file` are the deliberate
  exceptions. ACP takes no image input: attachments are written under the
  scratch cwd's `refs/<session>/`, named in the prompt, seen through
  `read_file` and passed to `image_edit` by path; they are removed when the
  session closes. A client hook registered on `session/new`
  (`_meta["x.ai/hooks"]`, answered on `_x.ai/hooks/run` from the reader
  thread) denies `read_file`/`image_edit` outside the refs dir and grok's
  session store, and denies every other tool. Grok fails open on a slow or
  malformed hook reply, so keep that handler fast and pure. Reject other
  agent-to-client requests with -32601. `XAI_API_KEY` must never reach the
  process. Sessions and
  generated images persist under `~/.grok/sessions/` by decision; do not
  delete them. The CLI has no subscription quota readout; the only metering
  is grok's per-turn cost estimate, which excludes images. Grok also loads
  Claude Code skills and hooks from `~/.claude`; `GROK_HOME` isolates that
  but moves `auth.json`, so it is not used.
- agy uses one persistent `agy acp --disable-telemetry --gemini-dir
  .gemini-agentry-<auth>` process, one `session/new` per chat, and sets the
  combined model id (`gemini-3.8-flash-high`) with `session/set_model`
  inside the turn lock. Auth is explicit (`--agy-auth`): `api-key` (default,
  `GEMINI_API_KEY` from env or `keys.ini`) or `oauth` (personal Google login,
  opt-in, warned at start; Google's terms §6 forbid third-party access).
  Each auth gets its own gemini dir: agy falls back to a saved login when
  the requested method lacks a credential, and the lockdown hook must never
  land in the user's `~/.gemini`. In oauth mode strip `GEMINI_API_KEY`,
  `GOOGLE_API_KEY` and `AGY_ADC_AUTH`. The tool restriction is the
  PreToolUse hook `agy_hook.py`, rewritten into `<gemini-dir>/config/
  hooks.json` at every start: it allows only the `image-generator`
  subagent, `generate_image` with references inside `brain/`, its
  `send_message`, and `read_file` inside `brain/`. No ACP permission request
  ever arrives, and without the hook agy reads cwd files unasked. agy fails
  closed on a missing, slow or garbage hook (validated on 1.5.9). Images
  come from an async subagent: after a turn that dispatched it, wait for a
  new file in `brain/<session>/`. Attachments ride the prompt as image
  blocks and are also written to `brain/<session>/refs/`, named in the
  prompt, and removed when the session closes. Sessions and generated
  images stay under the gemini dir; do not delete them.

## Request and Session Contracts

The HTTP layer forwards only the latest user message. System/developer
messages, client-supplied conversation history, and `max_tokens` are not
forwarded. Copilot and Codex retain backend conversation state; Claude does
not. A request with one user message and no assistant messages heuristically
starts a new chat. There is one global backend and a global cancel endpoint.
Do not describe this as full OpenAI compatibility or client-session isolation.

Model and reasoning effort are per-request values. Apply selection atomically
with the turn under the backend lock; omitted values use launcher defaults.
That is the intended contract; the audit records a gap for unpinned Copilot
defaults. Session-reset atomicity (audit A02) is closed for chat completions
since 2026-11-24: `_chat_lock` in `agentry.py` holds a request's new-chat
decision and its turn as one critical section. It is taken before the
backend's `turn_lock`, never inside a backend, and `/cancel` does not take
it; keep that order. Test the actual backend path, not just the HTTP stub. Session creation, selection, streaming, cancellation, and
cleanup must agree on which request owns a turn. Filter abandoned-turn events
and stop backend work when abandoning a response.

`prompt()` yields strings, `("reasoning", text)`, or
`("image", mime, base64, revised_prompt)`. Chat reasoning appears in
`delta.reasoning_content`; images appear in `delta.images`/`message.images`,
never mixed into answer text. Preserve these contracts in streaming and
non-streaming changes, and return structured errors for invalid requests.

Images endpoints run on Codex, Grok and agy (501 elsewhere), accept `n=1`
and `b64_json`, and use separate scratch threads or sessions serialized with
chat turns. Edits accept multipart or data URIs and reject masks; the
reference ceiling is the tool's own, five on Codex and three on Grok, and
agentry rejects above it with 400. agy's ceiling is not probed and not
enforced. On Grok the references are written under
the scratch cwd for `image_edit` and removed after the turn. `size` adds
advisory aspect wording; it does not guarantee exact pixels. Report the
actual delivered dimensions and format (Codex PNG, Grok JPEG) from the
image header, never from the model's prose. Do not delete Codex- or
Grok-owned generated images or session history.

The Videos routes (`/v1/videos`, OpenAI's Sora-era shape) are Grok-only and
run `image_to_video` on a throwaway session via `video_turn()`, which yields
`("video", mime, path, revised_prompt)`: a path, never bytes. A reference
image is mandatory (grok has no text-only video), generation is synchronous
and the object returns `completed` or `failed`; `/content` serves the file
grok wrote. The in-memory registry forgets ids on restart and `DELETE` never
removes the file. Report delivered size and duration from the MP4 boxes.
Chat completions must never carry video; the backend hook allows the video
tools only for the session `video_turn()` is running.

## UI, Logging, and Quota

Keep HTML/SVG/JavaScript artifacts in an iframe with `sandbox="allow-scripts"`
and without `allow-same-origin`. Markdown artifacts render in the main page:
escape attribute values and validate link schemes, not just HTML text. The
audit records an existing link injection defect. UI history is in memory;
new-chat and cancellation changes must account for an outstanding response.

Console logs currently include prompt previews; Codex/Claude wire logs can
contain full messages and image data. Treat logs as sensitive local artifacts;
do not claim they are redacted or include real user content in test fixtures.

Copilot quota uses an account baseline plus local SQLite usage since that
baseline, with a fresh throwaway runtime every 15 minutes to bypass the
runtime's quota cache. Preserve the distinction between account estimates,
local usage, and this-run usage. Codex credits are a dated token-rate estimate
that excludes full image cost; Claude passively reads an externally refreshed
quota cache. Do not present historical rates or probe results as current facts.

Consult `TODONT.md` before revisiting intentionally deferred features, checking
historical notes against the current implementation.

## Commit & Pull Request Guidelines

Use imperative, descriptive commit subjects, scoped by area when useful.
Keep commits focused and preserve unrelated working-tree changes. Do not
commit `venv/`, `logs/`, credentials, `.claude/memory/`, downloaded runtimes,
or generated local artifacts. PRs should explain the affected backend or UI
behavior, list verification performed, link relevant issues, and include
screenshots for visible UI changes.
