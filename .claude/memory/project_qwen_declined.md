---
name: project-qwen-declined
description: "qwen3-code is DECLINED as an agentry backend (not deferred) — Qwen sells a direct OpenAI-compatible API, so there is no subscription-locked model to unlock"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4458cf70-6310-41b7-8122-a4e1a038520d
  modified: 2026-07-28T11:05:54.442Z
---

2026-07-28: The user confirmed qwen3-code is **declined outright** as an agentry backend, not merely deferred. Recorded in `TODONT.md` and `TODO.md` (previously only said "unknown automation surface; no demand").

**Why:** Agentry's core premise is unlocking models that are only reachable through a coding-agent subscription (Copilot, ChatGPT/codex, Claude). Qwen's models are sold as a plain OpenAI-compatible API (DashScope / qwen.ai) to any subscriber — nothing to liberate, so wrapping the qwen CLI adds auth surface and support tail for a model you can already `curl`. Related earlier decision: [[project-antigravity-windows]] (antigravity = shelved/deferred, different reason: no Windows wheel, agy has no streaming mode).

**How to apply:** Don't propose qwen/qwen3-code as a backend candidate. Apply the same disqualification test to future candidates: if the vendor sells the model as a direct API, it fails agentry's bar regardless of CLI quality.
