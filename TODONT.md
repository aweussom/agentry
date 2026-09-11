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
TODO.md "Done"; `archive/CODEX-PLAN.md`). The `Backend` ABC in `backends.py` is the
plugin interface this entry said wasn't worth building — it was, once a
second backend justified it.

**Still deferred (the original reasoning, scoped to the rest):**
- `claude-code` (Anthropic) — `--output-format stream-json` is still
  `-p`-per-turn, NOT a persistent protocol. Wrapping it gives no spawn-cost
  win, which was the whole point of the persistent backend. Skip.
- `qwen3-code` — DECLINED (2026-07-28), not merely deferred. Qwen's models
  are sold as a plain OpenAI-compatible API (DashScope / qwen.ai) that any
  client can call directly with a subscription — there is no
  subscription-locked model for agentry to liberate, and unlocking
  agent-subscription models behind CLIs is this project's entire reason to
  exist. Wrapping the qwen CLI would add auth surface and a support tail to
  reach a model you can already `curl`. (The earlier note — unknown
  automation surface, no demand — remains true but is now moot.)
- `antigravity` / `agy` — DECLINED (2026-07-28) after three evaluation
  rounds, and NOT for technical reasons. By July 2026 it cleared every
  technical bar: SDK 0.1.8 ships Windows wheels; `agy -p` 1.0.2 grew
  `--output-format stream-json`, `--effort low|medium|high`, per-step
  usage (incl. cached tokens), and headless auto-deny of tool permissions
  (validated live: `run_command` → "User denied permission", clean ERROR
  step, no stall). Declined on economics and risk:
    1. The SDK can't reach the subscription quota at all — API-key/Vertex
       billing only, i.e. the plain Gemini API any client can buy. Fails
       the qwen3-code test.
    2. The sponsored CLI quota was gutted in March 2026: free "Starter
       Quota" is ~20 requests/day inside a weekly refresh window —
       unusable for enrichment. The only liberal paid pool (Flash on a
       5h refresh, Pro $20/mo) buys roughly what Copilot's free tier
       already provides for $0.
    3. Ban risk is asymmetric: Google suspended entire Google accounts —
       including paid Ultra subscribers — for driving subscriptions
       through third-party tools (Feb 2026 ban wave), and a personal
       Gmail account is attached here. GitHub and Anthropic tolerate the
       gray zone; Google demonstrably does not.
  The former watch items (SDK #20 credential reuse, CLI #31 `--acp`) are
  dropped: even if they land, (2) and (3) stand.
- General caution still holds: each backend adds CLI surface, auth
  gotchas, and a support tail. Add a backend only when it clears the bar
  codex did — a clean persistent JSON/streaming protocol AND a distinct
  audience (codex's: the paid-cheap ChatGPT Go/Plus tier vs Copilot's free
  tier).

Re-evaluate a specific candidate if it ships a persistent stdio protocol on
par with ACP / codex app-server.

## Polling `account/getQuota` on a TTL from a long-lived Copilot runtime

Idea (landed 2026-09-01, 888fa7a/92528dd): drive the heartbeat's
`Copilot credits N/5,000 used this month` line by re-calling
`client.rpc.account.get_quota` every 120 s from the console's own runtime.

**Verdict:** does not refresh. The Copilot runtime caches the quota answer
for the **lifetime of its process**; agentry's TTL only re-reads that cache.

**Why not** (measured 2026-09-11):
- The console runtime started 2026-09-08 09:32 and read 190/5,000 then. It
  printed `190/5,000 used · 4,810 left` at 06:55 on 09-11 while its own
  `this run` counter stood at 4,809.53, the local ledger at 4,922.85 for the
  month, copilot-cli's statusline at `Plan: 100% used`, and a *fresh*
  process's `getQuota` at 5,000/5,000. The refresh RPC ran cleanly every
  two minutes the whole time (`logs/copilot_sdk.log`) — it just returned
  the day-old snapshot.
- Same-process re-fetch 40 s apart returned a byte-identical `resetDate`
  (`05:03:07.217Z` both times). `resetDate` is the runtime's *fetch*
  timestamp, so identical stamps = served from cache. The 2026-09-01
  "moves within seconds of a turn" observation came from `_bench/quota_probe.py`,
  which spawns a fresh runtime per run — that is why it looked live.
- Passing `git_hub_token` in the request DOES hit the network (fresh stamp
  5 s later) but needs the Copilot OAuth token, which lives in the OS keyring
  under `tommyl_qfree`; `gh auth token` is the `aweussom` account with no
  Copilot plan and answers 0/0. Digging the runtime's token out of the
  keyring is not a fix worth owning.

What landed instead (2026-09-11): the snapshot is a baseline and the local
ledger supplies everything billed since its fetch stamp; the periodic re-fetch
spawns a throwaway runtime so it observes the account for real. See TODO
("Copilot plan quota went stale") and `_plan_quota_line` in `backends.py`.
