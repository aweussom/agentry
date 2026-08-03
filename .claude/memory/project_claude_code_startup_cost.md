---
name: project_claude_code_startup_cost
description: "2026-05-31 bench — claude-code backend startup cost is ~1.3s (persistent) / ~2.5s (cold), well within the 40-90s enrichment budget."
metadata: 
  node_type: memory
  type: project
  originSessionId: 6e0a13ba-2161-4312-ae5c-d4f10fc23f79
---

2026-05-31 measurement of claude-code's "startup cost" (oppstartskostnad) as a potential agentry backend #3. Chosen default model: **Sonnet 4.6** (`claude-sonnet-4-6`). Trivial prompt ("Reply with exactly: OK") to isolate overhead from generation.

Numbers (median, n=5):
- **Persistent + lean (`--strict-mcp-config`, no MCP): ~1.3s/task** (TTFT 1.2s). One long-lived `claude -p --input-format stream-json --output-format stream-json` process fed newline-delimited user messages over stdin — the apples-to-apples analog of the copilot `--acp` / codex `app-server` backends.
- Cold start (one `claude -p` process per task, lean): **~2.5s** (2.2-2.6s).
- Default config (all MCP servers: Atlassian/Figma/chrome) adds 1-2s to early turns and ~doubles cold start. `--strict-mcp-config` alone (no `--mcp-config`) loads zero servers — a real lever for a chat-only backend.
- Node boot alone (spawn->init): 0.47s, one-time.

**Why:** Tommy's Nynorsk exam enrichment runs 40-90s/task; he said 5-10s startup was tolerable. Actual is ~1.3-2.5s — comfortably under budget either way.

**How to apply:** claude-code is viable as backend #3. Two gotchas baked into `_bench/claude_probe.py`: (1) launch `claude.exe` WITHOUT `shell=True` — it's a real exe, not a .cmd shim, and cmd.exe wrapping mangles the stdout pipe (unlike copilot/codex which need shell=True). (2) In stream-json INPUT mode claude emits nothing — not even system/init — until the first user message arrives, so don't block waiting for init before sending. Cold mode may actually be preferable for independent tasks: zero context bleed, still in budget; persistent mode shares one conversation across turns so you'd need per-task session isolation. Quota: `quota_status()` reads the user's claude-code-quota cache (`~/.claude/quota-data.json`) — see [[reference_claude_code_quota]] (now wired in). Relates to [[project_agentry_backend_tiers]], [[project_codex_prompt_caching]].
