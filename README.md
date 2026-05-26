# Agentry

**Agentry removes the `-p` overhead of using CLI coding-tools for automation.**

[![A manic developer in a Norwegian sweater smashing an acoustic guitar into a laptop, keyboard keys flying out of the soundhole. The whiteboard reads "DAGENS PLAN: 1. Fikse litt på søk ✓ 2. Legge til AI ✓ 3. En liten proxy ✓ 4. ??? 5. Profit (kanskje)"](./images/dev-to-article-header.png)](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de)

If you'd rather read the unhinged origin story than boring sysadmin-grade
docs, the [dev.to version is here](https://dev.to/tommy_leonhardsen_81d1f4e/i-built-an-openai-compatible-proxy-for-github-copilot-because-search-was-too-stupid-to-understand-31de) —
it covers why this proxy exists in the first place (short answer:
Norwegian guitar tabs and questionable life choices). The rest of this
README is the boring documentation, which sysadmins know to love.

It holds one coding-agent CLI subprocess persistent across requests, drives it
over the Agent Client Protocol (JSON-RPC over stdio), and exposes the result
as an OpenAI-compatible HTTP API on localhost. Per-turn latency drops from
~8 s in `-p` mode to roughly the model's own `api_ms` floor (~2–3 s for short
replies with `gpt-5-mini` at `low` reasoning effort).

A minimal chat **web UI** ships with the proxy. It is not the point of the
project — just a quick way to confirm the API works end-to-end. The launcher
prints a clickable URL (`http://localhost:8765` by default) on startup.

![Bundled chat UI talking to the proxy as a regular OpenAI endpoint; footer shows the active backend and per-turn latency](./images/web-ui.png)

Currently wraps **GitHub Copilot CLI** (`copilot --acp`); built to be extended
to other agent CLIs (`claude-code`, `qwen3-code`, `antigravity-cli`, `codex`).

## Status

Personal spike. Single-user. Not packaged or published. Auth uses your
existing CLI logins via the local credential store. Operates in the same
gray ToS zone as any project that wraps a vendor's interactive CLI as a
programmatic backend — use a non-critical account, don't expose externally,
keep volume modest.

## Quick start

Common prerequisites:

- Python 3.10+
- GitHub Copilot CLI on PATH and logged in (`copilot login`)
- Node.js 18+ (the CLI is distributed as `@github/copilot` on npm)

### Windows (PowerShell 7+)

The launcher must be run from the same logon session as your interactive
`copilot login` so the cred-store token is reachable to child processes.

```powershell
cd C:\devel\aweussom\python\agentry
.\start.ps1                                          # gpt-5-mini, reasoning=low, port 8765
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
./start.sh                                           # gpt-5-mini, reasoning=low, port 8765
./start.sh --model claude-haiku-4.5 --reasoning-effort medium
./start.sh --port 9000
```

Open `http://localhost:8765` (or the port you chose) and chat. From a WSL2
shell, the same URL works from a Windows browser thanks to automatic port
forwarding.

## Configuration

Launcher params:

- `-Port` — HTTP port (default `8765`).
- `-Model` — passed to `copilot --model`. On the free tier: `gpt-5-mini`
  (default), `gpt-4.1`, `claude-haiku-4.5`. On paid tiers more models
  appear (Claude Sonnet 4.6, Opus 4.7, GPT-5.x family).
- `-ReasoningEffort` — applied via the ACP `session/set_config_option`
  method after `session/new`. Confirmed working: `low`, `medium`, `high`.
  Edge values (`none`, `xhigh`, `max`, `minimal`) appear in either the CLI
  or the API but never both, so they're not reliably reachable.

The web UI has a per-request reasoning-effort dropdown that overrides the
launcher default at runtime.

## Architecture

Single Flask file (`agentry.py`, ~400 lines) holds one persistent
`copilot --acp` subprocess and drives it with a small JSON-RPC 2.0 client.
ACP messages used:

| Direction | Method | Purpose |
|---|---|---|
| client → agent | `initialize` | handshake; declare capabilities |
| client → agent | `authenticate` | follow-up when `authMethods` is non-empty |
| client → agent | `session/new` | create chat session |
| client → agent | `session/set_config_option` | per-session reasoning override |
| client → agent | `session/prompt` | send user turn |
| agent → client | `session/update` (notification) | streamed `agent_message_chunk` deltas |
| client → agent | `session/cancel` (notification) | stop in-flight turn |

Agent→client requests for tools / permissions / filesystem are auto-denied
with JSON-RPC `-32601` to keep the proxy a pure chat client (no agent
capabilities — by design).

OpenAI-compatible endpoints exposed:

- `GET /health` — readiness probe.
- `GET /v1/models` — single model entry reflecting current `-Model`.
- `POST /v1/chat/completions` — SSE streaming, standard OpenAI delta format.
- `POST /v1/cancel` — sends `session/cancel` to the active turn.

The web UI at `/` is a single-page chat copied and pared down from the
NoLlama project. Markdown rendering, code-block copy, think-block support
(currently inert — Copilot CLI does not surface reasoning traces).

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
| `agentry.py` | Flask server + ACP JSON-RPC client |
| `templates/index.html` | Web UI shell |
| `static/css/style.css` | Web UI styles |
| `static/js/app.js` | Web UI client logic |
| `.github/copilot-instructions.md` | Per-project chat-only instructions |
| `start.ps1` | PowerShell launcher (creates venv, runs agentry) |
| `requirements.txt` | Just `flask` |
| `TODO.md` | Roadmap and known polish items |
| `logs/` | Runtime traces (gitignored): `acp_wire.log` |

## Known limits

- **Tool requests are always denied.** If a prompt genuinely needs a tool
  (file read, shell command), the agent will either degrade gracefully or
  error out rather than working around it. By design.
- **No visible reasoning trace.** Copilot CLI does not emit
  `agent_thought_chunk` events for our session — only a typing indicator
  appears during server-side reasoning. This is a CLI limitation, not a
  proxy limitation.
- **Auth is session-bound.** The Windows credential store entry that
  `copilot login` writes is reachable only to processes in the same
  interactive logon session. Running the launcher from a different shell
  or service account will fail to find the token.
- **Single user, single session.** Concurrent UI tabs share one ACP
  session and serialize through one turn lock. Fine for personal use; not
  a multi-tenant design.

## Roadmap

See `TODO.md`. The big next item is evaluating alternative backends
(`claude-code`, `qwen3-code`, `antigravity-cli`, `codex`) for the same
persistent-wrapper treatment, and choosing whether to make Agentry
multi-backend or fork per-CLI.

## Related work

[`ericc-ch/copilot-api`](https://github.com/ericc-ch/copilot-api) is a more
mature project that also exposes GitHub Copilot through an OpenAI-shaped
API. The two solve overlapping problems with different framings:

- *copilot-api* reverse-engineers Copilot's HTTP/WebSocket endpoints
  directly and is built to be a general-purpose API gateway for any client.
- *Agentry* drives the official `copilot --acp` JSON-RPC interface and is
  built for one specific use case: killing the per-call startup cost when
  a single developer uses Copilot CLI as an automation backend for their
  own scripts.

If you want a polished, broadly-applicable Copilot-as-an-API, copilot-api
is the more capable project. If you specifically want a thin local
persistent wrapper around the official agent CLI with no
reverse-engineering and a narrower scope, that is agentry.

## Acknowledgments

- [Agent Client Protocol](https://agentclientprotocol.com) by Zed Industries.
- Web UI based on the [NoLlama](https://github.com/aweussom/NoLlama)
  project (an OpenVINO-based LLM server for Intel NPU/GPU).
