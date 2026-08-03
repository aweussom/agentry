---
name: project-agentry-in-prod
description: Agentry is now running in production; all testing must use a non-standard port.
metadata: 
  node_type: memory
  type: project
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

As of 2026-05-30, agentry is deployed in production. Any local testing or
experiments must run on a **non-standard port** (not the default agentry
serving port) to avoid disturbing the live instance.

**Why:** A collision on the standard port would interfere with prod traffic.

**How to apply:** When launching agentry for tests (e.g. `start.ps1` /
`agentry.py`), pass an explicit alternate port. Note: the codex
`app-server` backend probe is stdio-only (no port), so it is inherently
safe; this constraint matters for the web-UI server. Relates to
[[project_codex_backend_investigation]].
