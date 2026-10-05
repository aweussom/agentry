# Claude startup improvement — 2026-09-23

Implemented in `ClaudeCodeBackend`, validated on Windows with Claude Code
2.1.281 and `claude-sonnet-4-6`. Subscription authentication was retained.

## What changed

- Reuse a `claude -p --input-format stream-json --output-format stream-json`
  process instead of spawning for every request.
- Before each subsequent task, send `/clear` and require both
  `conversation_reset` and a successful reset `result`, within five seconds.
  Consuming the terminal result prevents it from finishing the next task early.
- Perform reset and generation under the same turn lock, independently of the
  HTTP new-chat heuristic. Every task retains the old Claude backend's isolated
  conversation semantics; this does not introduce conversational chat history.
- Restart on model changes, including returning to the launcher default after
  a per-request override. If reset fails, restart and disable reuse for the
  backend's remaining lifetime instead of retrying an unsupported reset forever.
- Retire/reap workers on cancellation, timeout, early EOF, failed results or
  generator closure. Queues belong to individual processes. Failed output after
  partial text is surfaced, not silently converted to a successful empty reply.
- Use `--safe-mode --strict-mcp-config --tools "" --no-session-persistence`.
  This skips customizations and removes all tool schemas while keeping normal
  authentication. It deliberately disables custom hooks/plugins/memory for the
  relay. Do not substitute `--bare`, which the installed CLI says excludes OAuth.
- Effort and image forwarding are unchanged: both remain unimplemented for
  Claude. A modern CLI has `--effort`; the older comments claiming otherwise
  have been corrected.

## Measurements

The initial comparison used three samples per condition, one model, an empty
temporary cwd, and tiny prompts. Cold conditions started a fresh stream-JSON
process with either the previous lean flags or the new flags. Durations end
at the terminal result; they include inference, network and cache variability,
not just local startup. No production server was restarted.

| Condition | Observed total seconds | Median seconds |
|---|---|---:|
| Previous lean flags, fresh process | 1.997, 1.445, 1.548 | 1.548 |
| New safe/tool-free flags, fresh process | 1.686, 1.387, 2.015 | 1.686 |
| Persistent new flags, reset + turn | 0.883, 1.446, 0.715 | 0.883 |

The persistent samples include one codeword-recall prompt (expected `NONE`)
and two `OK` prompts, so the 43% median difference is indicative, not a
controlled performance guarantee. Reset durations were 0.193, 0.013 and
0.019 seconds. The new flag set alone did not establish a latency improvement.
It did reduce the observed input context: the old setup used approximately
14,121 input tokens including cache tokens, versus 3,698 for new cold turns.
Warm resets still reused about 3,098-3,799 cached input tokens; reset did not
discard all prefix-cache benefit in this run. These counts are machine/model/
version-specific; safe mode and tool removal were changed together.

A separate acceptance run exercised the actual Flask handlers and implemented
backend, including streaming and non-streaming responses. The first request
planted a codeword (`OK`, 1.243 s); the next returned `NONE` (1.332 s) using the
same process. Three further identical `OK` requests, each with reset included,
took **0.637, 1.930, 1.418 seconds** (median **1.418 s**). This confirms reuse and
isolation end-to-end but also shows substantial latency noise. Removing a
process launch per warm task is established; a fixed speedup percentage is not.

## Reproduction

These are live model calls, not offline tests:

```powershell
.\venv\Scripts\python.exe -u _bench/claude_startup_bench.py --samples 3
.\venv\Scripts\python.exe -u _bench/claude_startup_bench.py --implementation --samples 3
```

The benchmark requires successful result events and exact expected answers;
a silent CLI, timeout, failed reset or missing answer fails the run. Unlike
the older reset probe, it does not infer isolation from a missing answer.

Offline regression coverage lives in `test_claude_backend.py`. The broader
audit sweep remains separate; this change addresses Claude lifecycle defects
where necessary for safe reuse, not the other backends or general HTTP errors.
