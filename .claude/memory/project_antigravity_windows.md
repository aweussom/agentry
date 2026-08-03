---
name: project-antigravity-windows
description: "Antigravity SDK now HAS a Windows wheel (0.1.8) and a clean in-process Agent API, but is API-key-only auth — can't ride the sponsored agy quota, so still shelved as an agentry backend"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4458cf70-6310-41b7-8122-a4e1a038520d
  modified: 2026-07-28T11:26:21.461Z
---

**Updated 2026-07-28 (re-evaluation; supersedes the 2026-05-26 state).**

- The old blocker is GONE: `google-antigravity` 0.1.8 (2026-07-23) ships `win_amd64`/`win_arm64` wheels; weekly releases since 0.1.0. Bundles `localharness.exe` (~120 MB Go harness) inside the wheel.
- The SDK API is technically ideal for agentry: in-process `google.antigravity.Agent` (async context manager, no subprocess JSON-RPC), `Agent.chat()` with streamed `chunks`, `CapabilitiesConfig(enabled_tools=[])` for native tool-stripping, safety-policy enforcement, per-turn `UsageMetadata` with cached-token counts, ~2.3s session startup. Smoke-tested on native Windows in a scratch venv.
- **New blocker: auth.** `LocalAgentConfig` auth fields are `api_key`/`vertex`/`project`/`location` only. With no key it silently picks up `GEMINI_API_KEY` from env (which IS set on this box, tied to a depleted AI Studio prepay account → 429 RESOURCE_EXHAUSTED). No OAuth/subscription/CLI-credential path — it cannot use the sponsored `agy` quota, so it fails the same disqualification test as [[project-qwen-declined]]: the plain Gemini API is directly purchasable, nothing locked to liberate.
- The `agy` CLI still has no ACP/server mode (feature request: antigravity-cli issue #31, open, uncommitted). SDK credential-reuse request: antigravity-sdk-python issue #20, open, uncommitted.

**Final verdict, later on 2026-07-28: DECLINED — watch items dropped.** A live `agy -p` probe passed the last technical bars (1.0.2 has `--output-format stream-json` with per-step usage, `--effort low|medium|high`, and headless auto-deny of tool permissions — `run_command` cleanly refused). Declined anyway per the user's decision, sourced from Claude Desktop research: (a) the sponsored quota was gutted March 2026 — free "Starter Quota" ≈20 requests/day in a weekly refresh window, unusable for enrichment; the only liberal paid pool (Flash, 5h refresh, Pro $20) ≈ what Copilot free tier gives for $0; (b) Google banned entire Google accounts (incl. paid Ultra) for driving subscriptions via third-party tools (Feb 2026 ban wave), and the login here is the user's personal Gmail. GitHub/Anthropic tolerate the gray zone; Google doesn't. Same test as [[project-qwen-declined]] on leg (a), plus vendor-hostility risk on leg (b).

**Why:** Recorded in TODONT.md (full reasoning), TODO.md (evaluation trail), README roadmap (one line), all 2026-07-28.

**How to apply:** Don't propose antigravity/agy as a backend even if SDK #20 or CLI #31 lands — the quota economics and account-ban risk stand independently of protocol quality. Beware: `GEMINI_API_KEY` in this box's env makes the SDK silently bill paid AI Studio credits; unset it in any Gemini-adjacent experiment. Note the vendor-tolerance axis for future candidates: a backend is only viable if the vendor tolerates third-party subscription use.
