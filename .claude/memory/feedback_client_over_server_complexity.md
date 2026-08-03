---
name: feedback-client-over-server-complexity
description: "For agentry, prefer pushing complexity to clients rather than the server, even when both work"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b6d73901-2f64-498f-aaf6-97773d8dd45b
---

When a feature would work either as agentry-server logic or as client-side logic, prefer the client side. Keep the proxy minimal.

**Why:** User explicitly said this on 2026-05-28 when killing the `--instructions <markdown>` idea (see [[project-copilot-no-prompt-caching]] and `TODONT.md`): the ergonomic win of pinning prompts server-side wasn't worth the added CLI flag, file plumbing, and port-required validation when clients already know which prompt to send and can version it themselves. Server stays a thin OpenAI-compatible relay; clients carry their own context.

**How to apply:**
- Before proposing a new agentry CLI flag, config file, or per-route behavior, ask: can the client just send this in the request? If yes, default to that.
- Don't pitch "server-side convenience" as a feature on its own — it has to pay for the added server surface (testing, docs, edge cases).
- Exception: if the feature requires server state the client can't see (e.g., the persistent ACP session itself), server-side is justified.
