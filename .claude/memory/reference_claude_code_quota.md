---
name: reference-claude-code-quota
description: "claude-code-quota (aweussom/claude-code-quota) is NOW wired into agentry's claude backend via its ~/.claude/quota-data.json cache."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

The user's quota extractor: **https://github.com/aweussom/claude-code-quota**
(cloned locally at `C:/devel/aweussom/python/claude-code-quota`).

**Status 2026-05-31: WIRED IN.** When claude-code landed as agentry backend #3
(cold-start, see [[project_claude_code_startup_cost]]), `ClaudeCodeBackend.quota_status()`
was hooked to read the tool's cache file `~/.claude/quota-data.json` (fields:
`quota_used_pct`, `weekly_used_pct`, `resets_in`, `weekly_resets`, `valid`,
`stale`). Passive file read — no network, no dependency; if the tool isn't
installed the read fails and the backend falls back to the coarse per-turn
`rate_limit_event` (status + reset window, no %). Renders like the codex line:
`claude quota | 5h 54% left (resets in 31m) | weekly 74% left (resets in 1d15h)`.

How the extractor works: keeps `~/.claude/quota-data.json` fresh off claude's
own status-line ticks (TTL 60s active / 5min idle), no daemon. The same cache
the `quota` skill reads.

Relates to [[project-codex-backend-investigation]] and
[[project-codex-prompt-caching]] (codex quota is read from app-server
notifications; copilot's remaining-% is a live GitHub API value not persisted
to disk).
