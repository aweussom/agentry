---
name: project-codex-agent-tooluse
description: codex app-server is an AGENT — it intermittently shells out to explore cwd on real prompts; agentry mitigates with empty scratch cwd + chat-only developerInstructions.
metadata: 
  node_type: memory
  type: project
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

The codex backend's app-server is a coding agent, not a chat model. On real
enrichment prompts it **intermittently** uses its built-in shell tool to
explore the working directory for context — observed grepping the agentry repo
for JSON field names lifted from the prompt (`rg`, `Get-ChildItem` in
`codex_core::tools::router` errors, 2026-05-30).

Why the obvious guards don't stop it:
- `approvalPolicy: "never"` AUTO-APPROVES commands (it means "never ask"), so
  codex runs them without a client round-trip.
- `sandbox: "read-only"` still permits read commands.
- agentry's `-32601` client-deny only covers requests codex sends to the
  client; the shell executor is internal to app-server.

**Mitigation in `CodexAppServerBackend` (commit 304d27b):** two layers —
(1) run codex in an EMPTY scratch dir (`tempdir/agentry-codex-scratch`), never
the agentry repo, so there's nothing to find and agentry's source is out of
reach; (2) `developerInstructions` on `thread/start` telling codex to answer as
a stateless chat assistant and not use tools / read files.

**Verification status:** NOT empirically proven. The shell-out is intermittent
and did not reproduce in 8 isolated single-task trials (even pre-fix) — it's
batch/task-specific. Output quality holds (valid JSON at high effort).
`_bench/codex_toolcheck.py` flags commandExecution/fileChange/mcpToolCall items
per turn. Real verification = a full `--shuffle` batch run watching for
`codex stderr` / `tools::router` lines.

**Escalation if it recurs:** the only hard stop is to remove the agent harness
via `baseInstructions` on `thread/start` (replaces codex's base system prompt
rather than appending — see [[project_codex_prompt_caching]] / CODEX-PLAN.md),
which turns codex into a plain chat model. Behavior change, untested.
