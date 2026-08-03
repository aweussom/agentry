---
name: dont-ask-mode-no-shell
description: "User may run Claude Code in \"don't ask\" mode — Bash/PowerShell AND Edit/Write auto-denied (read-only tools fine); ask user to switch to accept-edits mode."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d4b5431d-4907-4807-9467-8bcd5b6ee6a4
  modified: 2026-07-27T08:10:38.091Z
---

In "don't ask" mode, permission prompts are disabled and anything not allowlisted is auto-DENIED (opposite of bypass-permissions). Bash and PowerShell bounce, and — confirmed 2026-07-27 — Edit bounces too: only read-only tools (Read/Glob/Grep) are usable. The user calls the mode "Auto" and assumed it let work proceed; when told it blocks edits, they switched to "accept edits on", which unblocks Edit/Write AND shell.

**Why:** User expected "don't ask"/"Auto" to mean auto-approve and was surprised (2026-07-22, re-confirmed 2026-07-27). An earlier version of this memory wrongly claimed Edit/Write still work in that mode.

**How to apply:** If Edit or shell gets auto-denied, don't work around it — stop and tell the user what's blocked; they'll happily switch to accept-edits mode. Analysis/read-only work can proceed meanwhile. See [[agentry-prod]] — tests must use a non-standard port anyway.
