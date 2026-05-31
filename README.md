# Agentry

**Agentry removes the `-p` overhead of using CLI coding-tools for automation.**

[![A manic developer in a Norwegian sweater smashing an acoustic guitar into a laptop, keyboard keys flying out of the soundhole. The whiteboard reads "DAGENS PLAN: 1. Fikse litt på søk ✓ 2. Legge til AI ✓ 3. En liten proxy ✓ 4. ??? 5. Profit (kanskje)"](./images/dev-to-article-header.png)](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de)

If you'd rather read the unhinged origin story than boring sysadmin-grade
docs, the [dev.to version is here](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de) —
it covers why this proxy exists in the first place (short answer:
Norwegian guitar tabs and questionable life choices). The rest of this
README is the boring documentation, which sysadmins know to love.

It holds one coding-agent CLI subprocess persistent across requests, drives it
over JSON-RPC 2.0 (stdio), and exposes the result as an OpenAI-compatible HTTP
API on localhost. Per-turn latency drops from ~8 s in `-p` mode to roughly the
model's own `api_ms` floor (~2–3 s for short replies with `gpt-5-mini` at
`low` reasoning effort).

A minimal chat **web UI** ships with the proxy. It is not the point of the
project — just a quick way to confirm the API works end-to-end. The launcher
prints a clickable URL (`http://localhost:8765` by default) on startup.

![Bundled chat UI talking to the proxy as a regular OpenAI endpoint; footer shows the active backend and per-turn latency](./images/web-ui.png)

Wraps two interchangeable backends, selected with `--backend`:

- **`copilot`** (default) — GitHub Copilot CLI (`copilot --acp`). The free
  tier; `gpt-5-mini` at `low`/`high` reasoning.
- **`codex`** — OpenAI Codex (`codex app-server`). The paid-but-cheap tier
  (ChatGPT Go $8 / Plus $20); default `gpt-5.4-mini` at `low` effort.

Both speak persistent JSON-RPC 2.0 over stdio, so both get the no-spawn-cost
win. Adding a third backend is implementing one `Backend` class in
`backends.py`.

## The idea: reverse MCP

The Model Context Protocol (MCP) standardizes one direction: how a model
*consumes* external capabilities. A host app embeds an LLM and connects to MCP
servers that expose tools and data — the arrow points `LLM ──▶ tools`.

Agentry points the arrow the other way. A coding-agent CLI is, by design, an
MCP *client*: it exists to call tools. Agentry wraps that agent and serves the
model as a plain HTTP endpoint, so ordinary software consumes the model instead:

```
MCP       LLM  ───▶  tools / data      (the model consumes capabilities)
agentry   code ───▶  LLM  (HTTP)       (your code consumes the model)
```

The agent built to call tools becomes the tool. And agentry *enforces* the flip
rather than just narrating it: every tool, permission, or filesystem request the
agent tries to make back to its host is denied (JSON-RPC `-32601`). Stripped of
its ability to consume tools, the agent is left as a pure language service
behind an OpenAI-shaped API.

A lens, not a protocol: agentry speaks the OpenAI chat API, not MCP, and does
not interoperate with MCP tooling. The point is the direction of the arrow —
turning a tool-using agent into a tool other programs use — not a shared wire
format.

## Status

Working tool. Used by the author as the primary enricher across adjacent
projects (`shiny-fiesta`, soon `geomap`). Two backends:
`copilot` (free) and `codex` (paid-cheap) — see `TODONT.md` for which other
CLIs were considered and declined, and why codex cleared the bar they didn't.
Auth uses your existing CLI logins via the local credential store. Operates
in the same gray ToS zone as any project that wraps a vendor's interactive
CLI as a programmatic backend — use a non-critical account, don't expose
externally, keep volume modest.

## Quick start

Common prerequisites:

- Python 3.10+
- For the `copilot` backend: GitHub Copilot CLI on PATH and logged in
  (`copilot login`); Node.js 18+ (distributed as `@github/copilot` on npm).
- For the `codex` backend: OpenAI Codex CLI on PATH and logged in
  (`codex login`, ChatGPT account — no `OPENAI_API_KEY` needed).

### Windows (PowerShell 7+)

The launcher must be run from the same logon session as your interactive
`copilot login` so the cred-store token is reachable to child processes.

```powershell
cd C:\devel\aweussom\python\agentry
.\start.ps1                                          # copilot, gpt-5-mini, reasoning=low
.\start.ps1 -Backend codex                           # codex, gpt-5.4-mini, effort=low
.\start.ps1 -Model claude-haiku-4.5 -ReasoningEffort medium
.\start.ps1 -Port 9000
```

![Launcher console: Flask boots, the copilot --acp subprocess is spawned, and the ACP handshake / auth / session / reasoning override all complete in well under a second — then every subsequent chat request lands on the same warm process](./images/startup-console.png)

### Linux / WSL2 Ubuntu

Install the CLI inside your Linux environment (in WSL2, install inside
WSL — not via Windows host):

```bash
npm install -g @github/copilot
copilot login

cd ~/path/to/agentry
chmod +x start.sh                                    # first checkout only
./start.sh                                           # copilot, gpt-5-mini, reasoning=low
./start.sh --backend codex                           # codex, gpt-5.4-mini, effort=low
./start.sh --model claude-haiku-4.5 --reasoning-effort medium
./start.sh --port 9000
```

Open `http://localhost:8765` (or the port you chose) and chat. From a WSL2
shell, the same URL works from a Windows browser thanks to automatic port
forwarding.

## Configuration

Launcher params:

- `-Port` — HTTP port (default `8765`).
- `-Backend` — `copilot` (default) or `codex`.
- `-Model` — model override.
    - `copilot`: passed to `copilot --model`. Free tier: `gpt-5-mini`
      (default), `gpt-4.1`, `claude-haiku-4.5`. Paid tiers add more
      (Claude Sonnet 4.6, Opus 4.7, GPT-5.x family).
    - `codex`: passed on `turn/start`. Default `gpt-5.4-mini`.
- `-ReasoningEffort` — `low`, `medium`, `high` are confirmed end-to-end on
  both backends. On `copilot` it is applied via ACP `session/set_config_option`
  after `session/new`; on `codex` it is a `turn/start` param. Edge values
  (`none`, `xhigh`, `max`, `minimal`) are not reliably reachable on copilot.

The web UI has a per-request reasoning-effort dropdown that overrides the
launcher default at runtime.

### Console

When idle, the launcher console pulses a `...*...*...*` heartbeat that rewrites
itself in place once a second (no scroll). On the `codex` backend it also drops
a permanent quota line into scrollback when going idle and every ~10 min after —
e.g. `codex go quota | weekly 22% left (resets 06 Jun 10:51)` — and appends the
remaining quota to each turn's completion line. The figure is primed at startup
(`account/rateLimits/read`) and kept fresh by the rate-limit snapshots codex
pushes per turn, so there are no extra API calls during normal use. (Output
redirected to a file suppresses the heartbeat; the permanent quota lines still
appear.)

#### copilot quota (opt-in)

The `copilot` backend can show your monthly Copilot **premium-request** quota,
read from the documented GitHub billing API
(`GET /users/{username}/settings/billing/premium_request/usage`). Enable it by
copying `agentry.ini.template` to `agentry.ini` and filling in a fine-grained
PAT (Account → **Plan: read-only**), your username, and plan tier. The console
then shows, e.g.:

```
copilot pro | premium 142/300 (53% left, resets in 9d)
```

cached for 10 minutes, with an optional PAT-expiry warning (`expiry`/`pat_name`
in the ini). It's **off by default** (blank `pat`) — the default model
`gpt-5-mini` doesn't consume premium requests, so for the default setup there's
nothing to meter. Note the figure is a *global* monthly total across all your
Copilot usage (CLI, IDE, agentry), not agentry's own.

**Caveat — personal billing only.** This works only for accounts that pay their
own Copilot bill (verified end-to-end against a personally-billed account). If
your license is **org/enterprise-managed** (e.g. an SSO / enterprise-managed
user), per-user billing is not exposed by GitHub's user-level API — it returns
HTTP 400/403 — and agentry disables the display automatically with one log line.
There is no user-level path for enterprise-managed seats.

## Architecture

`agentry.py` is the Flask layer (routes, OpenAI shape, session reuse).
`backends.py` holds a `Backend` ABC and the two implementations, each
owning one persistent subprocess driven by a small JSON-RPC 2.0 client.
The Flask layer talks only to the `Backend` interface
(`new_session` / `prompt` / `cancel` / `update_reasoning_effort` /
`is_alive` / `close`), so swapping backends is a `--backend` flag.

The two protocols map almost one-to-one:

| Concept | `copilot` (ACP) | `codex` (app-server) |
|---|---|---|
| Handshake | `initialize` (+`authenticate`) | `initialize` |
| New session | `session/new` | `thread/start` |
| User turn | `session/prompt` | `turn/start` |
| Reasoning override | `session/set_config_option` | `effort` on `turn/start` |
| Streamed deltas | `session/update` → `agent_message_chunk` | `item/agentMessage/delta` |
| Turn complete | `session/prompt` result `stopReason` | `turn/completed` notification |
| Cancel | `session/cancel` | `turn/interrupt` |

In both, agent→client requests for tools / permissions / filesystem are
auto-denied with JSON-RPC `-32601` to keep the proxy a pure chat client (no
agent capabilities — by design). This denial is where the reverse-MCP inversion
is enforced: the agent's tool-consuming half is switched off, leaving only the
language model to be served. Codex additionally runs each thread with
`approvalPolicy: never` + `sandbox: read-only`.

OpenAI-compatible endpoints exposed:

- `GET /health` — readiness probe.
- `GET /v1/models` — single model entry reflecting current `-Model`.
- `POST /v1/chat/completions` — SSE streaming, standard OpenAI delta format.
- `POST /v1/cancel` — sends `session/cancel` to the active turn.

The web UI at `/` is a single-page chat copied and pared down from the
NoLlama project. Markdown rendering, code-block copy, think-block support
(inert on `copilot`, which does not surface reasoning traces; `codex` does
emit `item/reasoning/textDelta` events, not yet wired into the UI).

## Per-project instructions

`.github/copilot-instructions.md` is loaded by Copilot for each ACP session
it opens in this directory. It tells the agent: treat prompts as standalone
chat questions, do not read repo files, do not request tools, do not inject
system reminders or context hints, be terse. This overrides any global
custom-instructions file in `~/.copilot/` that would otherwise leak hints
(SQL tables, todo lists, etc.) into prompts.

## File map

| Path | Purpose |
|---|---|
| `agentry.py` | Flask server + OpenAI surface + backend selection |
| `backends.py` | `Backend` ABC + `CopilotACPBackend` + `CodexAppServerBackend` |
| `logutil.py` | Shared timestamped logging + idle keepalive |
| `templates/index.html` | Web UI shell |
| `static/css/style.css` | Web UI styles |
| `static/js/app.js` | Web UI client logic |
| `.github/copilot-instructions.md` | Per-project chat-only instructions |
| `start.ps1` / `start.sh` | Launchers (create venv, run agentry) |
| `requirements.txt` | Just `flask` |
| `TODO.md` | Roadmap and known polish items |
| `TODONT.md` | Paths intentionally not taken, with reasons |
| `archive/CODEX-PLAN.md` | Codex backend design + validation record (archived) |
| `logs/` | Runtime traces (gitignored): `acp_wire.log`, `codex_wire.log` |

## Known limits

- **Tool requests are always denied.** If a prompt genuinely needs a tool
  (file read, shell command), the agent will either degrade gracefully or
  error out rather than working around it. By design.
- **Reasoning trace depends on backend.** Copilot CLI does not emit
  `agent_thought_chunk` events for our session — only a typing indicator
  appears during server-side reasoning. Codex *does* stream reasoning
  (`item/reasoning/textDelta`), but agentry does not yet forward it to the
  UI.
- **Auth is session-bound.** The Windows credential store entry that
  `copilot login` writes is reachable only to processes in the same
  interactive logon session. Running the launcher from a different shell
  or service account will fail to find the token.
- **Single user, single session.** Concurrent UI tabs share one ACP
  session and serialize through one turn lock. Fine for personal use; not
  a multi-tenant design.
- **Codex carries a fixed ~24.8k-token core harness per turn.** codex
  app-server sends its agent system prompt + built-in tool schemas on every
  turn. It does not leak into responses, is not reducible by disabling
  plugins/MCP/skills (tested), and is cached server-side so it costs almost
  no latency. On a flat ChatGPT subscription it isn't separately billed.

## Roadmap

See `TODO.md` for active polish items and `TODONT.md` for paths
intentionally not taken. The backend layer is now pluggable and ships two
backends (`copilot`, `codex`). Further backends (`claude-code`,
`qwen3-code`, `antigravity`) remain deferred — `claude-code` is
`-p`-per-turn with no persistent protocol, so wrapping it buys no
spawn-cost win, and the others were shelved for the reasons in `TODONT.md`.
A new backend now means implementing one `Backend` class, not a refactor.

## Related work

[`ericc-ch/copilot-api`](https://github.com/ericc-ch/copilot-api) is a more
mature project that also exposes GitHub Copilot through an OpenAI-shaped
API. The two solve overlapping problems with different framings:

- *copilot-api* reverse-engineers Copilot's HTTP/WebSocket endpoints
  directly and is built to be a general-purpose API gateway for any client.
- *Agentry* drives the official agent CLIs through their published JSON-RPC
  stdio protocols — `copilot --acp` (ACP) and `codex app-server` — and is
  built for one specific use case: killing the per-call startup cost when a
  single developer uses these CLIs as an automation backend for their own
  scripts.

The two-backend design also means agentry isn't tied to one vendor: the same
OpenAI-shaped endpoint fronts either Copilot (free tier) or Codex (cheap paid
tier), swapped with a flag. If you want a polished, broadly-applicable
Copilot-as-an-API, copilot-api is the more capable project. If you
specifically want a thin local persistent wrapper around the official agent
CLIs with no reverse-engineering and a narrower scope, that is agentry.

## Acknowledgments

- [Agent Client Protocol](https://agentclientprotocol.com) by Zed Industries.
- Web UI based on the [NoLlama](https://github.com/aweussom/NoLlama)
  project (an OpenVINO-based LLM server for Intel NPU/GPU).
