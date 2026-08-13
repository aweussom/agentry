# Agentry

**Point your OpenAI SDK at the coding-agent subscription you already pay for.**
*The agent built to call tools becomes the tool.*

Agentry wraps a coding-agent CLI — GitHub Copilot, OpenAI Codex, or Claude
Code — holds it as one persistent process, and serves the model behind it as
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
`markdown` blocks. It is not the point of the project, just proof the API
works end-to-end. The launcher prints the URL (`http://localhost:8765`).

![Bundled chat UI talking to the proxy as a regular OpenAI endpoint: markdown answer with a copy button on the code block, a collapsible thinking block above it, a per-turn backend + latency tag, image attach, and a header showing the active model and reasoning effort](./images/web-ui.png)

> **Intended use:** a personal, localhost-only adapter. Each backend stays
> authenticated through its own official client and remains subject to that
> provider's terms — agentry adds no access path, credentials, or multi-user
> service on top. The `copilot` backend rides the official Copilot SDK, a
> supported product surface; `codex` and `claude` wrap interactive CLIs
> programmatically and sit in the usual gray ToS zone — use a non-critical
> account there, keep volume modest, and never expose the port publicly.

## The idea: reverse MCP

MCP standardizes one direction: how a model *consumes* tools
(`LLM ──▶ tools`). Agentry points the arrow the other way. A coding-agent CLI
is an MCP *client* — it exists to call tools. Agentry confiscates them and
serves the bare model back out, so ordinary software consumes the model
instead (`code ──▶ LLM`).

The flip is enforced, not narrated: every backend runs with its tool surface
switched off. The copilot session is created with an empty tool allowlist and
a deny-all permission handler; codex and claude get every tool, permission,
and filesystem request refused at the wire (JSON-RPC `-32601`), and codex
threads additionally run `approvalPolicy: never` + `sandbox: read-only`.
Stripped of its ability to consume tools, the agent is left as a pure
language service behind an OpenAI-shaped API.

## Backends

Selected with `--backend`. Adding one means implementing a single `Backend`
class in `backends.py`.

| Backend | Wraps | Cost tier | Defaults |
|---|---|---|---|
| `copilot` (default) | the official [Copilot SDK](https://github.com/github/copilot-sdk) | Copilot AI credits per token (1 credit = $0.01); `gpt-5.6-luna` is the cheap band ($0.20/M in), ~10× under `terra`, ~25× under `sol` | `gpt-5.6-luna` @ `low` |
| `codex` | `codex app-server` (persistent JSON-RPC stdio) | ChatGPT Go $8 / Plus $20; Codex credits per token, `luna` 25× cheaper than `sol` | codex's own configured model @ `low` |
| `claude` | `claude -p`, one fresh process per turn | Claude subscription (premium) | `claude-sonnet-4-6` |

`copilot` and `codex` hold one persistent runtime process, so turns cost only
model latency. Claude Code has no server mode, making `claude` a
**cold-start** backend (~2.5 s spawn per turn) — built for long single-shot
tasks (40–90 s enrichment turns) where the spawn is noise and per-turn
isolation is a feature.

## Quick start

Prerequisites: Python 3.11+ plus the CLI login for the backend you use:

- `copilot` — `copilot login` once from any installed Copilot CLI. The SDK
  downloads its own pinned native runtime (Node.js not needed to *run*
  agentry) and reads the same `~/.copilot` credential store.
- `codex` — Codex CLI on PATH, `codex login` (ChatGPT account, no API key).
- `claude` — Claude Code CLI on PATH and logged in; `-p` never prompts.

**Windows (PowerShell 7+)** — run from the same logon session as your
interactive `copilot login`, or the credential-store token is unreachable:

```powershell
.\start.ps1                          # copilot, gpt-5.6-luna, effort=low
.\start.ps1 -Backend codex           # model from codex config
.\start.ps1 -Backend claude
.\start.ps1 -Port 9000
```

**Linux / WSL2** — log in inside your Linux environment (in WSL2: inside WSL,
not via the Windows host; npm is only needed for this login step):

```bash
npm install -g @github/copilot && copilot login     # one-time
./start.sh                           # flags: --backend codex|claude, --port 9000
```

Open `http://localhost:8765` and chat. From WSL2 the same URL works in a
Windows browser via automatic port forwarding.

![Launcher console: the SDK client starts in under two seconds, reports the authenticated GitHub login, opens a session, and settles into the idle heartbeat — every subsequent chat request lands on the same warm process](./images/startup-console.png)

## Configuration

Launcher params (`start.ps1 -Flag` / `start.sh --flag`):

- **Port** — HTTP port, default `8765`.
- **Backend** — `copilot` (default), `codex`, or `claude`.
- **Model** — override the default.
  - `copilot`: set as the SDK session's model, validated against your plan's
    `models.list`. The available set tracks Copilot's plans — check your
    model picker, not this README.
  - `codex`: sent per `turn/start`. When unset, each thread runs whatever
    `~/.codex/config.toml` says — and the codex TUI *writes your last picked
    model there*, so agentry silently follows TUI switches unless you pin
    `-Model`. Deliberate (it tracks OpenAI's model migrations for free), but
    pin for prod. The startup log's `codex thread: ... (default model=...)`
    line shows what each thread resolved to.
  - `claude`: passed to `claude --model`.
- **ReasoningEffort** —
  `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra`. What applies
  is per model: copilot's gpt-5.6 models advertise `none`→`max`, codex takes
  `none`→`ultra` (`ultra` is codex-only: "maximum reasoning with automatic
  task delegation"); a model that rejects a level keeps its previous one
  (WARN, not an error). **No-op on `claude`** — `-p` exposes no effort knob.

### Per-request model and effort

Launcher values are only defaults. Like a normal OpenAI endpoint, model and
effort are **request fields, not server state** — each turn runs on exactly
what its request asked for, applied atomically with the turn:

- `"model"` — copilot: the live session is switched under the turn lock
  (conversation history preserved); codex: a `turn/start` param; claude: the
  spawn's `--model`. Ids are validated against the account's model list;
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
  DeepSeek-popularized extension; standard clients ignore it).
- `POST /v1/cancel` — cancels the in-flight turn (copilot `session.abort()`,
  codex `turn/interrupt`, claude kills the process).

## Architecture

`agentry.py` is the Flask layer (routes, OpenAI shape, session reuse);
`backends.py` holds the `Backend` ABC and the three implementations. Flask
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
| `backends.py` | `Backend` ABC + copilot / codex / claude implementations |
| `logutil.py` | Timestamped logging + idle heartbeat/ticker |
| `templates/`, `static/` | Web UI |
| `.github/copilot-instructions.md` | Per-project chat-only instructions |
| `start.ps1` / `start.sh` | Launchers (create venv, run agentry) |
| `TODO.md` / `TODONT.md` | Roadmap / paths intentionally not taken |
| `archive/` | Backend design + validation records |
| `logs/` | Runtime wire traces (gitignored) |

## Known limits

- **Tool requests are always denied** — by design. A prompt that genuinely
  needs a tool degrades or errors rather than working around it.
- **Reasoning trace depends on backend.** Copilot's summaries reach the
  console ticker and the web UI think-block; codex also streams reasoning
  but it is not forwarded yet.
- **Auth is session-bound** (Windows): the credential-store entry from
  `copilot login` is only reachable from the same interactive logon session.
- **Single user, single session.** Concurrent clients share one backend
  session and serialize through one turn lock — personal use, not
  multi-tenant.
- **Codex carries a fixed ~24.8k-token harness per turn** (its agent system
  prompt + tool schemas). Not reducible, cached server-side, not separately
  billed on a flat subscription.

## Related work

[`ericc-ch/copilot-api`](https://github.com/ericc-ch/copilot-api) (and its
maintained fork
[`caozhiyuan/copilot-api`](https://github.com/caozhiyuan/copilot-api)) expose
Copilot by reverse-engineering its internal HTTP endpoints — wider scope,
lower overhead, permanent exposure to upstream breakage. Agentry instead
drives the official runtimes through their supported surfaces (Copilot SDK,
`codex app-server`) for one narrow case: killing the per-call startup cost
when a single developer uses these agents as an automation backend. Want a
broad multi-client gateway? Pick a maintained copilot-api fork. Want a thin
local wrapper with no reverse-engineering? That's agentry.

## Acknowledgments

- [Agent Client Protocol](https://agentclientprotocol.com) by Zed Industries —
  the copilot backend's original transport, since replaced by the Copilot SDK.
- Web UI based on [NoLlama](https://github.com/aweussom/NoLlama) (an
  OpenVINO-based LLM server for Intel NPU/GPU).
