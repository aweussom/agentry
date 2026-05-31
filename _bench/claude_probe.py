"""Minimal claude-code probe: measure the STARTUP COST of driving `claude` (the
Claude Code CLI) as an agentry backend. Structural counterpart to codex_probe.py
(`codex app-server`) and bench.py (`copilot --acp`).

We are NOT measuring inference quality or throughput here — only overhead. So the
prompt is trivial ("Reply with exactly: OK") and the model is the smallest/fastest
(haiku), to drive the model's own generation time toward zero and leave wall-clock
dominated by startup: process boot, config/MCP load, auth, and one API round-trip.

claude-code has no persistent JSON-RPC "server" mode like codex app-server or
copilot --acp. The two realistic ways to use it as a backend are:

  cold    one `claude -p "<prompt>"` process PER task. Simplest to wire up, but
          every task re-pays Node boot + config/MCP load + auth. Worst case.

  persist one long-lived `claude -p --input-format stream-json` process fed
          newline-delimited user messages over stdin (the Agent-SDK transport).
          Node boot + MCP load are paid ONCE at spawn; each task then costs only
          the API round-trip. This is the apples-to-apples analog of how the
          Copilot/Codex backends already work in agentry.

Orthogonally, default `claude` loads every configured MCP server (Atlassian,
chrome-devtools, ...) and the full tool set at startup — pure overhead for a
chat-only backend. `--strict-mcp-config` with no `--mcp-config` loads zero MCP
servers; we measure with and without it to size that lever.

Usage:
    python _bench/claude_probe.py cold    <n> [model] [--lean]
    python _bench/claude_probe.py persist <n> [model] [--lean]

    --lean  add --strict-mcp-config (no MCP servers) + tool restrictions, i.e.
            the config a real chat backend would run with.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

PROMPT = "Reply with exactly: OK"
# Sonnet 4.6 is the chosen default for the claude-code backend. A trivial prompt
# still keeps generation near-zero, so wall-clock stays dominated by startup.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Flags that turn claude into a stateless chat answerer: ignore filesystem MCP
# config (load none), forbid every tool, and skip the project's settings. This
# is what a real agentry "claude" backend would launch with.
# --strict-mcp-config alone (no --mcp-config) => load zero MCP servers. Avoids
# passing inline JSON through a shell=True cmdline, which Windows would mangle.
LEAN_FLAGS = ["--strict-mcp-config",
              "--disallowedTools", "Task,Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit"]


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def fmt(xs):
    return ",".join(f"{x:.3f}" for x in xs)


# --- mode: cold (one `claude -p` process per task) -----------------------

def run_cold(n, model, lean):
    cmd_base = ["claude", "-p", PROMPT, "--model", model]
    if lean:
        cmd_base += LEAN_FLAGS
    totals = []
    for i in range(1, n + 1):
        t0 = time.monotonic()
        # claude resolves to a real claude.exe (not a .cmd shim), so no shell
        # wrapper is needed — and cmd.exe wrapping mangles the stdout pipe.
        p = subprocess.run(cmd_base, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        total = time.monotonic() - t0
        out = (p.stdout or "").strip().replace("\n", " ")[:60]
        if p.returncode != 0:
            err = (p.stderr or "").strip().replace("\n", " ")[:120]
            print(f"  call {i}: rc={p.returncode} total={total:.2f}s  ERR {err!r}", flush=True)
        else:
            print(f"  call {i}: total={total:.2f}s  -> {out!r}", flush=True)
        totals.append(total)
    return totals


# --- mode: persist (one long-lived stream-json process, N turns) ---------

class ClaudeStream:
    """Drive `claude -p --input-format stream-json --output-format stream-json`
    as a persistent process. Each newline-delimited user message is one turn."""

    def __init__(self, model, lean):
        cmd = ["claude", "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--include-partial-messages",  # emit content_block_delta for true TTFB
               "--verbose",                    # required with stream-json output in -p
               "--model", model]
        if lean:
            cmd += LEAN_FLAGS
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        self.q = queue.Queue()
        self.spawn_t = time.monotonic()
        self.init_t = None       # wall-clock when the system/init event arrived
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _reader(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init" \
                        and self.init_t is None:
                    self.init_t = time.monotonic()
                self.q.put((time.monotonic(), msg))
        except Exception:
            pass

    def _stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line and line.strip():
                    print(f"[stderr] {line.rstrip()[:200]}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def spawn_to_init(self):
        """Seconds from process spawn to the one-time system/init event. In
        stream-json INPUT mode claude stays silent until the first user message
        arrives, so this is only populated after the first turn() — it folds the
        Node boot + config/MCP load into the first turn. None if never seen."""
        return None if self.init_t is None else self.init_t - self.spawn_t

    def turn(self, text, timeout=180):
        """Send one user message; return (ttft_s, total_s, reply). ttft = time to
        first assistant text delta; total = time to the turn's result event."""
        # drain any stragglers
        while not self.q.empty():
            self.q.get_nowait()
        payload = {"type": "user", "message": {"role": "user",
                   "content": [{"type": "text", "text": text}]}}
        t0 = time.monotonic()
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        ttft = None
        reply = []
        while True:
            try:
                ts, msg = self.q.get(timeout=timeout)
            except queue.Empty:
                return (ttft, time.monotonic() - t0, "".join(reply) + " [TIMEOUT]")
            mtype = msg.get("type")
            if mtype == "stream_event":
                ev = msg.get("event") or {}
                if ev.get("type") == "content_block_delta":
                    d = (ev.get("delta") or {}).get("text")
                    if d:
                        if ttft is None:
                            ttft = ts - t0
                        reply.append(d)
            elif mtype == "assistant":
                # Fallback when partial messages aren't emitted: whole message.
                for blk in (msg.get("message") or {}).get("content", []):
                    if blk.get("type") == "text" and blk.get("text"):
                        if ttft is None:
                            ttft = ts - t0
                        if not reply:
                            reply.append(blk["text"])
            elif mtype == "result":
                return (ttft, ts - t0, "".join(reply).strip())

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def run_persist(n, model, lean):
    cs = ClaudeStream(model, lean)
    try:
        ttfts, totals = [], []
        for i in range(1, n + 1):
            ttft, total, reply = cs.turn(PROMPT)
            tt = f"{ttft:.2f}s" if ttft is not None else "n/a"
            extra = ""
            if i == 1:
                s2i = cs.spawn_to_init()
                extra = f"  [spawn->init {s2i:.2f}s, folded into this turn]" if s2i else ""
            print(f"  turn {i}: ttft={tt} total={total:.2f}s  -> {reply[:50]!r}{extra}", flush=True)
            if ttft is not None:
                ttfts.append(ttft)
            totals.append(total)
        return cs.spawn_to_init(), ttfts, totals
    finally:
        cs.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("cold", "persist"):
        print(__doc__)
        sys.exit(1)
    mode = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    rest = sys.argv[3:]
    lean = "--lean" in rest
    rest = [a for a in rest if a != "--lean"]
    model = rest[0] if rest else DEFAULT_MODEL

    print(f"=== claude {mode}  model={model} lean={lean}  n={n} ===", flush=True)

    if mode == "cold":
        totals = run_cold(n, model, lean)
        print(f"  SUMMARY claude-cold: total median={median(totals):.2f}s "
              f"min={min(totals):.2f}s max={max(totals):.2f}s", flush=True)
        print(f"BENCH_RESULT mode=cold model={model} lean={lean} n={len(totals)} "
              f"totals={fmt(totals)}", flush=True)
    else:
        spawn_to_init, ttfts, totals = run_persist(n, model, lean)
        if ttfts:
            print(f"  SUMMARY claude-persist: spawn->init={spawn_to_init:.2f}s | "
                  f"per-turn ttft median={median(ttfts):.2f}s | "
                  f"total median={median(totals):.2f}s "
                  f"min={min(totals):.2f}s max={max(totals):.2f}s", flush=True)
        print(f"BENCH_RESULT mode=persist model={model} lean={lean} n={len(totals)} "
              f"spawn_to_init={spawn_to_init:.3f} ttfts={fmt(ttfts)} totals={fmt(totals)}",
              flush=True)


if __name__ == "__main__":
    main()
