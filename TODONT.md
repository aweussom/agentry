# TODONT

Things that work but aren't worth doing. Each entry explains *why not* so we
don't re-litigate.

## `--instructions <markdown>` flag on agentry

Idea: let agentry pin a system prompt from a markdown file (with `--port`
required when `--instructions` is set, so it's a deliberate choice). One
agentry instance per "enrichment" type, hardcoded instructions per server.

**Verdict:** functional but pointless.

**Why not:**
- No backend caching benefit on Copilot. 2026-05-28 bench (`_bench/`)
  compared a 101 B vs 29 651 B `.github/copilot-instructions.md` across
  5 `session/new` + `session/prompt` cycles each, identical prefix every
  turn. Median TTFB: 7.56s vs 15.12s — long prefix is ~2× slower with no
  amortization across calls. The backend re-processes the prefix every
  turn rather than caching it.
- Adds server complexity (CLI flag, file plumbing, port-required
  validation, swapping `.github/copilot-instructions.md`) for an
  ergonomic win that belongs *client-side*: the client orchestrating
  enrichment already knows which prompt to send and is the right place
  to version it.
- If a use case ever needs server-side instruction pinning (multiple
  consumers sharing one config), the existing
  `.github/copilot-instructions.md` already covers it — just edit that
  file in the cwd agentry runs from.

**Update 2026-05-30 (codex backend):** the *caching* objection above is
Copilot-specific. A bench (`_bench/codex_cache_bench.py`) found codex pays
NO TTFB penalty for a 28 KB instruction prefix (median 6.52s vs 8.69s for a
101 B prefix) — the OpenAI Responses API caches the static prefix
server-side. So "static prefix is dead weight" is false on codex. The entry
still stands, but now on the *second* reason only: instruction pinning
belongs client-side (the client orchestrating enrichment already knows which
prompt to send and is the right place to version it). Latency is no longer a
reason to avoid it on codex.

Re-evaluate if: a use case genuinely needs server-side instruction pinning
shared across multiple consumers AND runs on the codex backend — then the
caching win makes it cheap, and only the client-side-ownership argument
remains.

## Multi-backend support — NARROWED 2026-05-30 (codex landed)

This entry originally deferred *all* additional backends indefinitely. That
was the right call when every candidate was either `-p`-per-turn
(`claude-code`) or parser-fragile (`agy`). It changed when codex shipped
`codex app-server`: a persistent stdio JSON-RPC protocol, a near-direct
structural match for Copilot's ACP. **Codex is now a landed backend** (see
TODO.md "Done"; `CODEX-PLAN.md`). The `Backend` ABC in `backends.py` is the
plugin interface this entry said wasn't worth building — it was, once a
second backend justified it.

**Still deferred (the original reasoning, scoped to the rest):**
- `claude-code` (Anthropic) — `--output-format stream-json` is still
  `-p`-per-turn, NOT a persistent protocol. Wrapping it gives no spawn-cost
  win, which was the whole point of the persistent backend. Skip.
- `qwen3-code` — unknown automation surface; no demand.
- `antigravity` / `agy` — evaluated and shelved 2026-05-26 (no Windows
  wheel; `agy -p -c` reprints the full transcript per turn, no streaming).
- General caution still holds: each backend adds CLI surface, auth
  gotchas, and a support tail. Add a backend only when it clears the bar
  codex did — a clean persistent JSON/streaming protocol AND a distinct
  audience (codex's: the paid-cheap ChatGPT Go/Plus tier vs Copilot's free
  tier).

Re-evaluate a specific candidate if it ships a persistent stdio protocol on
par with ACP / codex app-server.
