---
name: project-agentry-backend-tiers
description: "Agentry's two-tier backend positioning — free=Copilot CLI, paid-cheap=codex on $8/$20 ChatGPT; no codex free-tier dependency."
metadata: 
  node_type: memory
  type: project
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

Agentry's backend story is now three tiers:
- **Free** → Copilot CLI backend (gpt-5.5-mini), the existing default. Its
  quota is actually fairly liberal.
- **Paid-but-cheap** → codex `app-server` backend, via ChatGPT Go ($8/mo) or
  Plus ($20/mo). For the token volume agentry can pull through app-server,
  $20/mo is far cheaper than any metered API.
- **Premium** → claude backend (`claude -p`, COLD-START, default Sonnet 4.6),
  landed 2026-05-31. ~2.5s startup overhead per task (no persistent stdio
  protocol; see [[project_claude_code_startup_cost]]). Directly aimed at the
  demanding nynorsk exam enrichment below that gpt-5.4-mini couldn't do and
  DeepSeek V4 Pro is only "barely good enough" for — that's the whole reason
  this tier exists.

**Why:** A documented codex *free*-tier quota isn't really available, so the
"free AI for colleagues" pitch does NOT rest on it. Poor users stay on
Copilot CLI; anyone wanting codex pays the cheap ChatGPT subscription. User's
recollection: codex on Plus had ~5× the headroom of claude-code on Pro a few
months ago.

**How to apply:** Don't treat "codex free-tier quota" as a blocking unknown —
it's moot. This clears the last decision gate in [[project_codex_backend_investigation]];
the `Backend` protocol refactor in archive/CODEX-PLAN.md is warranted. Default
codex model is gpt-5.4-mini @ low effort.

**Real-world (2026-05-31):** codex backend validated end-to-end against
PRODUCTION — works fine. The economics tension that surfaced:
- **gpt-5.4-mini @ low (the default) is good enough for MOST tasks** — but NOT
  for the demanding outlier the user actually needed it for: enriching
  complicated **nynorsk** exam questions. Good model, just not for that.
- Switching to a **stronger model gave the capability but drained the Go ($8)
  weekly quota FAST**, so the user stopped (the per-turn / heartbeat quota
  display surfaced it in time). (For that nynorsk task the user is now on
  **DeepSeek V4 Pro**, which is *barely* good enough — it's a genuinely hard
  task.)
So the bind isn't "the cheap default is rate-limited" — it's that the
capable-enough model is quota-expensive on Go. For sustained prod with a model
strong enough to do the job, **Plus ($20) is effectively required**; Go only
works with the weak default or light/dosed batches. The default model
(gpt-5.4-mini) is a fast/cheap *starting* point — override `--model` for real
enrichment quality. Not a code issue — economics + model-capability.
See [[project-agentry-quota-economics]].
