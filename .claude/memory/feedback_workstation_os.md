---
name: feedback-workstation-os
description: User stays on native Windows 11 on this workstation; do not suggest WSL2 as a workaround even when it would work
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 22fb4f79-875d-4c0e-9730-c867c0eaf299
---

This workstation runs Windows 11 and the user has decided to stay native — do not suggest WSL2 / Linux as a workaround for "this doesn't run on Windows" problems, even though WSL2 (Ubuntu + docker-desktop distros) is installed and reachable.

**Why:** User stated explicitly when I proposed WSL as a fallback for the missing Windows wheel of `google-antigravity`: "I'd rather not use WSL. I have decided to stay on Windows 11 on this workstation, if possible."

**How to apply:** When a tool/SDK lacks a Windows build, propose Windows-native alternatives first (a CLI binary, a different SDK shape, a pure-Python path). Only mention WSL as a last-resort escape hatch, and only after exhausting Windows-native options. Project already has a `start.sh` for Linux/WSL users, but that's for other users — not this workstation's default workflow.
