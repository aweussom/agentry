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

Re-evaluate if: a backend with real prompt caching becomes the target
(direct OpenAI/Anthropic), at which point "static prefix" actually pays —
but that loses Copilot's free-quota property.

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
