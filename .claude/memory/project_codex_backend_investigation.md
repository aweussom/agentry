---
name: project-codex-backend-investigation
description: codex app-server VALIDATED as backend #2 (2026-05-30 smoke-test passed); since 2026-07-27 model is inherited from codex config (was hardcoded gpt-5.4-mini, now deprecated); see CODEX-PLAN.md
metadata: 
  node_type: memory
  type: project
  originSessionId: b6d73901-2f64-498f-aaf6-97773d8dd45b
  modified: 2026-07-27T08:10:08.186Z
---

**Update 2026-07-27: model no longer hardcoded.** OpenAI deprecated
`gpt-5.4-mini` (migration target `gpt-5.6-luna`). `CodexAppServerBackend` now
defaults `model=None`: the `turn/start` override is omitted and threads run on
codex's own config default (`~/.codex/config.toml` `model`, i.e. the last codex
TUI selection). Probe on `gpt-5.6-luna` @ `low`: median TTFB 6.20s. NOTE the
2026-05-30 "unpinned = worse (8.86s)" result below meant *auto-route with no
config default*; with a concrete model in config.toml, omitting the override is
fine — it resolves to that model, not auto-routing. Side effect: changing the
model in the codex TUI silently changes agentry's codex tier on its next thread
(README documents this; pin with `--model` if it matters).

**Update 2026-05-30: smoke-test PASSED.** `_bench/codex_probe.py` drives
`codex app-server` over stdio end-to-end. Auth is ChatGPT-account login (no
API key). Default backend model decided: **`gpt-5.4-mini` at `low` effort** —
median TTFB 6.48s (tight 6.36–6.88s), beats the Copilot SHORT baseline 7.56s.
Leaving codex to auto-route (unpinned) gave worse, high-variance numbers
(8.86s median). The ONE remaining gate before the "free AI for colleagues"
pitch is **free-tier quota** (CODEX-PLAN.md step 5) — not yet checked.

The original investigation into adding `codex app-server` as a second backend
for agentry, alongside Copilot ACP. The design + validation record lives in
`archive/CODEX-PLAN.md` (moved there 2026-05-31 once codex landed) — that file
is the authoritative history.

**Why:** 2026-05-30 web research showed codex CLI has a `codex app-server` subcommand that speaks JSON-RPC 2.0 over stdio with a stateful subprocess — near-direct structural equivalent of Copilot's ACP (`thread/start` ≈ `session/new`, `turn/start` ≈ `session/prompt`, etc). This reverses the part of [[feedback-client-over-server-complexity]] / TODONT.md "Multi-backend support" that lumped codex in with claude-code. claude-code still doesn't have a persistent stdio protocol; codex does.

**How to apply:**
- If the user references "the codex thing" or "the multi-backend question", read `CODEX-PLAN.md` first — it has the protocol mapping, verification steps, and decision gates.
- Don't assume the TODONT.md "Multi-backend support" entry still applies — it was written before the codex app-server finding.
- The architecture refactor in TODO.md ("Factor backend out ... only if multi-backend pans out") becomes justified IF the codex smoke-test passes; until then it's still conditional.
- User was installing codex on Windows 11 and needed a PowerShell session restart for PATH pickup; next session likely begins with verifying the install.
