# Codex backend plan

**Status (2026-05-30): ALL GATES CLEARED — codex is a validated backend #2,
refactor warranted.** `codex-cli 0.135.0` installed (`codex.exe`, native
binary, no shim). Auth: logged in via ChatGPT account (no `OPENAI_API_KEY`
needed). The protocol probe (`_bench/codex_probe.py`) drives `codex
app-server` over stdio end-to-end.

**Quota gate resolved by positioning (not by a codex free tier):** agentry
keeps a free path via the existing Copilot CLI backend (gpt-5.5-mini, whose
quota is actually quite liberal). Codex is the *paid-but-cheap* tier — $8/mo
(Go) or $20/mo (Plus) ChatGPT. For the token volume `app-server` lets agentry
extract, $20/mo is far cheaper than any metered API (corroborated: codex on
Plus a few months ago had ~5× the headroom of claude-code on Pro). So agentry
does NOT depend on a codex free-tier quota — step 5 is moot.

## Results (2026-05-30)

Probe: spawn `codex app-server` → `initialize` → `thread/start` →
`turn/start` (prompt "Reply with exactly: OK") → collect
`item/agentMessage/delta` → `turn/completed`. Median of 5, fresh thread each
cycle. Stdio-only, so **no port collision with the live (prod) agentry**.

| Config | TTFB median | range | total median |
|---|---|---|---|
| gpt-5.4-mini @ low (pinned) | **6.48s** | 6.36–6.88s | 6.59s |
| default (auto-route) @ low | 8.86s | 6.33–12.78s | 9.00s |
| Copilot SHORT baseline (2026-05-28) | 7.56s | — | — |

**Decision: default backend model = `gpt-5.4-mini` low.** Pinning the model
beats the Copilot baseline on TTFB *and* removes the variance seen when codex
is left to auto-route across model tiers. This is now the default in
`codex_probe.py`.

Decision gates: smoke-test PASS ✓, TTFB competitive PASS ✓ (beats Copilot),
quota PASS ✓ (resolved by positioning — free=Copilot CLI, paid-cheap=codex on
$8/$20 ChatGPT; no codex free-tier dependency). **All gates cleared →
architecture refactor warranted.**

Two-tier product story:
- **Free** → Copilot CLI backend (gpt-5.5-mini), the current default.
- **Paid-cheap** → codex backend, ChatGPT Go ($8) or Plus ($20). Very cheap
  per-token given app-server throughput.

Base-context note (2026-05-30): a trivial codex turn reports ~24,789
inputTokens — codex app-server ships its core agent harness (system prompt +
built-in tool schemas) every turn. This is **not reducible** by disabling
marketplace plugins / MCP / skills: `plugins={}`, `mcp_servers={}`,
`experimental_use_skills=false` all leave the count at exactly 24,789, and no
MCP child process spawns either way (`_bench/codex_min_context.py`; `-c`
plumbing verified via a working `model` override). The desktop plugins aren't
loaded into the app-server turn path. The harness is cached server-side
(proven via `cachedInputTokens`, `_bench/codex_cache_proof.py`), so it's
latency-cheap and — on a flat ChatGPT subscription — not separately billed.
Don't add plugin-disable flags to the backend; they're a no-op here.

The ONLY lever that would actually shrink the 24.8k is `baseInstructions` on
`thread/start` (ThreadStartParams), which **replaces** codex's base system
prompt rather than appending to it (the way `developerInstructions` does).
This is NOT a free optimization: discarding codex's agent scaffolding
(tool-use protocol, safety, output conventions) turns codex into a plainer
chat model and is a behavior change, not a tuning knob. Untested — we don't
know how far it shrinks the prefix or what regresses. Only worth it if the
24.8k ever becomes a real constraint (context-window pressure, or a future
per-token-billed codex tier); today it's cached and free, so leave it alone.

Protocol confirmations (from `codex app-server generate-json-schema`, dumped
to `_bench/codex_schema/`): wire methods are exactly `initialize`,
`thread/start`, `turn/start`, `turn/interrupt`, `turn/steer`, `thread/resume`;
text streams via `item/agentMessage/delta` (params.delta), terminal signal is
`turn/completed`. Bonus: codex emits `item/reasoning/textDelta` /
`item/reasoning/summaryTextDelta` reasoning traces (Copilot has NO equivalent)
— answers a former open question. `model`/`effort` are turn-level params
(`turn/start`), NOT `thread/start` fields.

---

### Original plan (pre-validation, retained for context)

## Why this reopened

TODONT.md "Multi-backend support" was the right call when `claude-code`
was the candidate (no persistent stdio protocol — wrapping it gives no
speed win). But web search on 2026-05-30 surfaced that **codex has a
`codex app-server` subcommand: stateful subprocess, JSON-RPC 2.0 over
stdio, near-direct structural equivalent of Copilot's ACP.** That makes
codex a viable backend #2 in a way claude-code never was.

Pricing: codex has a free ChatGPT tier plus inclusion in Go ($8/mo) and
Plus ($20/mo). Free tier reach into "no Azure license" colleague segment
is plausible (subject to free-tier quota — needs verification).

## Protocol mapping

| Concept | Copilot ACP | Codex app-server |
|---|---|---|
| Session creation | `session/new` | `thread/start` |
| User turn | `session/prompt` | `turn/start` |
| Cancel in-flight turn | `session/cancel` | `turn/interrupt` |
| Append to active turn | — | `turn/steer` (bonus) |
| Resume past session | (recreated each time) | `thread/resume` (bonus) |
| Streamed text deltas | `session/update` (notif) `agent_message_chunk` | `item/agentMessage/delta` (notif) |
| Turn complete signal | `result` with `stopReason` | `turn/completed` (notif) |

JSON-RPC framing is identical (newline-delimited JSON over stdio).
Notification semantics differ in detail but not in shape.

Reference: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md

## Verification steps (next session, in order)

1. **Confirm install.** Fresh PowerShell:
   ```
   codex --version
   codex app-server --help
   ```
   If `codex` is not on PATH, troubleshoot install before continuing.

2. **Confirm auth.** Run `codex` once interactively to surface any login
   prompt (ChatGPT account vs API key). Note which auth model is in use.

3. **Smoke-test the protocol.** Write `_bench/codex_probe.py` modeled on
   `_bench/bench.py`. Minimum sequence:
   - Spawn `codex app-server`
   - Send `initialize` (or whatever codex requires — check `app-server`
     README for handshake)
   - `thread/start`
   - `turn/start` with prompt `"Reply with exactly: OK"`
   - Read notifications until terminal event; collect
     `item/agentMessage/delta` content
   - Record TTFB (time to first delta) and total time
   - Run 5 cycles; report median

4. **Compare with Copilot baseline.** `_bench/` already has a SHORT
   instructions baseline at median TTFB 7.56s (2026-05-28 bench). Codex
   should be in the same ballpark for the comparison to make sense.

5. **Check free-tier limits.** Find documented daily/monthly quota for
   the codex free tier. If it's tight (e.g. ~10 requests/day), the
   "free local AI for colleagues" use case dies even with a working
   backend.

## Decision gates

- **Smoke-test passes AND TTFB competitive AND free-tier quota is
  reasonable** → proceed to architecture refactor (next section).
- **Smoke-test fails OR codex significantly slower OR free tier too
  tight** → re-evaluate. Either codex moves into TODONT.md alongside
  claude-code, or stays as a future candidate.

## Architecture refactor (if go)

The conditional item in TODO.md ("Factor backend out of `agentry.py`
into a plugin interface ... premature until at least one second backend
is validated end-to-end") becomes warranted.

Extract a `Backend` protocol from current `ACPClient`:

```python
class Backend(Protocol):
    def new_session(self) -> str: ...
    def prompt(self, text: str) -> Iterator[str]: ...   # yields deltas
    def cancel(self) -> bool: ...
    def update_reasoning_effort(self, value: str) -> bool: ...
    def close(self) -> None: ...
```

Concrete implementations:
- `CopilotACPBackend` — current `ACPClient` renamed, no behavior change
- `CodexAppServerBackend` — new

Selection via `--backend {copilot,codex}` CLI flag on `start.ps1` /
`agentry.py`, default `copilot`. Each backend resolves its own model
list, reasoning levels, and auth.

Update docs:
- README.md: status section, file map, roadmap
- TODO.md: move "Architecture" item to "Done" if refactor lands
- TODONT.md: remove "Multi-backend" entry (or narrow it to "additional
  backends beyond copilot+codex deferred")

## Open questions

- Codex auth model for `app-server` mode — ChatGPT login (browser
  OAuth) or `OPENAI_API_KEY` env var? Documentation didn't specify in
  the search.
- Model selection — Copilot uses `--model gpt-5-mini`; codex equivalent
  for `app-server`?
- Reasoning effort equivalent in codex?
- Does codex `app-server` emit `agent_thought_chunk`-style events that
  would surface reasoning traces in the agentry UI (which Copilot does
  NOT)?
- Free-tier rate limits — see step 5 above.

## Files referenced

- `agentry.py` — current single-backend implementation
- `_bench/bench.py` — existing ACP bench, model for `codex_probe.py`
- `TODO.md` — has the conditional "Architecture" item
- `TODONT.md` — has the "Multi-backend support" entry that codex would
  partially reverse
- `README.md` — Status / Roadmap / file map need updates if codex lands
