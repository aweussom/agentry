---
name: project-codex-prompt-caching
description: PROVEN via cachedInputTokens — codex uses OpenAI prompt caching (unlike Copilot). Also: ~24k-token agent base context sent every turn (cached).
metadata: 
  node_type: memory
  type: project
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

2026-05-30 bench (`_bench/codex_cache_bench.py`): fresh `codex app-server`
thread each cycle, identical prefix passed as `developerInstructions`,
prompt "Reply with exactly: OK", 5 cycles each, gpt-5.4-mini @ low.

- short prefix (101 B):  median TTFB 8.69s (6.34–9.22)
- long prefix (28 KB):   median TTFB 6.52s (6.27–8.11) — NO penalty, even
  marginally faster.

This is the **opposite** of [[project_copilot_no_prompt_caching]], where a
28 KB prefix ~doubled TTFB every turn (15.12s vs 7.56s). Codex runs on the
OpenAI Responses API, which caches long static prefixes server-side, so a
big instruction prefix is effectively free after the first hit.

**Why it matters:** server-side instruction pinning is viable on the codex
backend in a way it never was on Copilot. The TODONT "`--instructions`
flag" entry's *caching* objection is Copilot-specific and does NOT apply to
codex; its other objection (pinning belongs client-side) still stands.

**PROOF 2026-05-30** (`_bench/codex_cache_proof.py`, reads
`thread/tokenUsage/updated` → `tokenUsage.last.{inputTokens,cachedInputTokens}`,
size curve 100B→100KB, 3 fresh threads each): `cachedInputTokens` is real and
large (often 75–99% of input). The 10KB row shows the textbook warm-up:
cold cycle 9% cached → warm 81% → 99.7%. And a natural cache-miss experiment:
100KB at 6.5s when cached vs 11.4s when a cycle missed (9.5% cached) — so cache
*state*, not prefix size, drives latency. Caching confirmed, not inferred.

**Surprise finding:** even a 100B developerInstructions prompt reports
inputTokens ≈ 24,800. codex app-server sends a ~24k-token agent base context
on EVERY turn. It does not leak into response text (model still replies "OK")
and reports no user memory, but it is there — cached, so latency-cheap, but it
dominates input-token cost.

**Disabling plugins does NOT trim it (tested 2026-05-30, `codex_min_context.py`):**
inputTokens stays exactly 24,789 with `plugins={}`, `mcp_servers={}`, and
`experimental_use_skills=false`. `-c` is confirmed working (a `-c model=...`
override changes the thread model), and no MCP child process spawns either
way. The ~24.8k is codex's CORE harness (system prompt + built-in tool
schemas); the desktop plugins aren't loaded into the app-server turn path.
So disabling plugins for agentry is a no-op — don't bother. (Earlier guess
that `-c` could trim it was wrong.)

**How to apply:** If a codex-backed use case wants a fixed long system prompt,
latency is not a reason to avoid it — the prefix is cached. The dominant input
cost is codex's own harness, not your instructions.
