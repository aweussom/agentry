---
name: project-copilot-no-prompt-caching
description: 2026-05-28 bench shows GitHub Copilot backend does not visibly cache identical system prompts via copilot --acp on gpt-5-mini
metadata: 
  node_type: memory
  type: project
  originSessionId: b6d73901-2f64-498f-aaf6-97773d8dd45b
---

Pinning a long system prompt in `.github/copilot-instructions.md` and reusing it across `session/new` cycles does **not** give detectable backend prompt-cache benefits on gpt-5-mini via `copilot --acp`.

**Why:** Bench on 2026-05-28 in `_bench/` (script: `_bench/bench.py`) compared 101 B vs 29 651 B instructions, 5 turns each, fresh `session/new` per turn (identical prefix every time). Median TTFB: 7.56s (short) vs 15.12s (long, ~2× slower). Calls 2–5 in long config showed no convergence toward short's level — bench backend re-processes the prefix each turn rather than caching it.

**How to apply:**
- Don't pitch "agentry-per-enrichment with pinned instructions" as a *speed* win — it's only ergonomic (single source of truth, clients don't pass system message).
- Every byte of instructions costs TTFB roughly linearly. Keep system prompts as small as possible.
- If real prompt caching matters, route past Copilot to direct OpenAI/Anthropic APIs — but that loses the free-quota property from [[project-agentry-vs-azure-llm]].
- Re-test if model changes (gpt-5, Claude via Copilot, etc.) — this finding is specific to gpt-5-mini + current Copilot backend.
