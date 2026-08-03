---
name: feedback-first-run-oauth
description: "When a fresh CLI tool hangs with near-zero CPU on first invocation under PowerShell tool, suspect a stuck OAuth/browser flow and have the user run it manually first"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 22fb4f79-875d-4c0e-9730-c867c0eaf299
---

When invoking an unfamiliar CLI tool via the PowerShell tool and the process consumes near-zero CPU but doesn't terminate for minutes, the most likely cause is a stuck first-run OAuth/browser flow — not slowness, not the model. The tool fires off a browser-based consent or local-callback flow that needs a foreground user interaction the headless Claude Code session cannot complete.

**Why:** First time we tried `agy.exe -p "..."` via PowerShell from Claude Code, it blocked for 4+ minutes consuming 0.1s CPU. I assumed agy was either slow or broken. When the user ran the exact same command in their own terminal, a browser window flashed open and the call completed in 7.6s; subsequent calls were 5.5s (clean). The 4-minute hang in the Claude Code session was an OAuth flow with no UI surface — silent forever.

**How to apply:** If a fresh CLI tool hangs without CPU activity, don't escalate timeouts and don't write it off as broken. Hand the exact command back to the user to run once in their terminal — that completes the auth, and from then on the same command works fine from Claude Code. Always check `agy --help` / `<tool> --help` for explicit `login` / `auth` subcommands first (agy didn't have one, hence the surprise). Same pattern likely applies to `gcloud`, `gh`, `az`, `vercel`, and any other vendor CLI.
