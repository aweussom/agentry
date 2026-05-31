# Claude Code backend plan

**Status (2026-05-31): LANDED as backend #3 (cold-start).** `claude` CLI 2.1.158
installed (`C:\Users\wossn\.local\bin\claude.exe`, a real exe — NOT a .cmd shim).
Auth: already logged in via the Claude Code CLI's own OAuth; `-p` runs headless
and never prompts. `ClaudeCodeBackend` in `backends.py`, selected with
`--backend claude`. Default model **Sonnet 4.6** (`claude-sonnet-4-6`).

## Why cold-start (and not a persistent server)

claude-code has **no persistent JSON-RPC server mode** — there is no analog of
copilot's `--acp` or codex's `app-server`. Its `-p` (print) mode runs one request
and exits. This was the original reason claude-code was deferred in TODONT.md:
"no speed win from wrapping." The 2026-05-31 startup-cost bench reopened it.

Bench (`_bench/claude_probe.py`, Sonnet 4.6, trivial prompt, median of 5):

| Mode | Per-task overhead | Notes |
|---|---|---|
| **cold + lean** (one `claude -p` per task) | **~2.5s** (2.2–2.6s) | what we shipped |
| persistent + lean (stream-json over stdin) | ~1.3s (TTFT 1.2s) | one shared conversation |
| persistent, default (all MCP) | ~2.0s, up to 3s early | MCP load drags early turns |
| Node boot alone (spawn→init) | 0.47s | one-time |

The target workload is Nynorsk exam enrichment, 40–90s/task, where 5–10s of
startup was stated as tolerable. Cold-start's ~2.5s is comfortably inside that
(3–6% overhead) and buys, for free, the thing persistent mode can't give:
**every turn is a brand-new conversation with zero context bleed.** For
independent enrichment tasks that isolation is exactly right, so cold-start is
the better fit here, not merely the easier one.

`--strict-mcp-config` (load zero MCP servers) is the real lever: it ~halves cold
start (default config loads Atlassian/Figma/chrome-devtools, all useless for a
chat relay) and removes 1–2s from early persistent turns. Combined with
`--disallowedTools` and an empty scratch cwd, claude runs as a stateless chat
answerer — the same defense-in-depth posture the codex backend uses.

## Persistent mode — deferred, reconsider if startup ever dominates

A persistent claude could be driven via the Agent-SDK transport:
`claude -p --input-format stream-json --output-format stream-json --verbose`,
fed newline-delimited user messages over stdin. Node boot + MCP load are paid
ONCE at spawn; each task then costs only the API round-trip (~1.3s vs ~2.5s).
Worth doing only if startup overhead ever becomes the bottleneck — e.g. a
high-volume batch of *short* turns where 1.2s/task × N actually adds up. For
40–90s enrichment turns the ~1.2s saving is noise, so it is not worth the
isolation cost below today.

### The leakage problem, and why prompting is not the fix

In stream-json input mode the process **is** the conversation: every turn
accumulates in one context window. There is no "new session" message in that
input protocol (unlike codex `thread/start` or copilot `session/new`, which
open a fresh server-side session on the *same* persistent process). So turn N
sees turns 1..N-1. For independent tasks that is leakage: cross-task influence
on the output, plus you re-pay the growing prefix in tokens/latency every turn.

**Can the client suppress it with prompting?** (Tommy's question, 2026-05-31.)
Only partially, and not reliably:
- A "treat each task independently / ignore prior messages" instruction *biases*
  the model but does not *remove* the prior turns — they are physically still in
  the context window. They keep consuming tokens, keep adding latency, and can
  still leak (especially with similar-looking exam tasks back to back).
- It is a soft guarantee at best. Cold-start's isolation is structural: a fresh
  process has no prior context to leak, period.

So prompting is a mitigation, not a solution. The clean way to isolate in
persistent mode is a fresh session per task — which for claude-code stream-json
means restarting the process per task, i.e. cold-start again. That circularity
is precisely why cold-start is the right default for independent tasks; persistent
mode only pays off when turns are genuinely part of one ongoing conversation
(e.g. an interactive chat in the web UI), where shared context is desired, not
leaked. If we ever add a persistent claude path, scope it to that case and keep
cold-start as the default for batch/enrichment.

## Quota

Wired into the existing `github.com/aweussom/claude-code-quota` tool, which
keeps `~/.claude/quota-data.json` fresh with the real OAuth usage % (5-hour
session + weekly), refreshed off claude's own status-line ticks — no daemon.
`quota_status()` reads that cache passively (no network, no dependency; if the
tool isn't installed the read just fails and we fall back), rendering e.g.
`claude quota | 5h 54% left (resets in 31m) | weekly 74% left (resets in 1d15h)`
— same shape as the codex quota line. Fallback when the cache is absent: the
coarse `rate_limit_event` claude streams each turn (status + reset window, no
%), e.g. `claude five_hour: allowed (resets 31 May 12:10)`.

## Files

- `backends.py` — `ClaudeCodeBackend` (cold-start) + `make_backend` wiring.
- `agentry.py` — `--backend claude`, `owned_by: anthropic`, docstring/help.
- `_bench/claude_probe.py` — the startup-cost bench (`cold`/`persist` × `--lean`).
- `archive/CODEX-PLAN.md` — sibling plan for backend #2.
