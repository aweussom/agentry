"""Minimal codex app-server probe: drive `codex app-server` over stdio (JSON-RPC
2.0, newline-delimited) to measure TTFB and total time per turn. Structural
counterpart to bench.py (which drives `copilot --acp`).

Protocol (confirmed from `codex app-server generate-json-schema`):
    initialize                 -> {clientInfo:{name,version}}
    thread/start               -> {model?, effort?, approvalPolicy, sandbox}; result.thread.id
    turn/start                 -> {threadId, input:[{type:"text",text}], model?, effort?}
    item/agentMessage/delta    (notif) streamed assistant text; params.delta
    item/reasoning/textDelta   (notif) streamed reasoning trace (Copilot has no equivalent)
    turn/completed             (notif) terminal signal; params.turn.status

Usage:
    python _bench/codex_probe.py <label> <n> [model] [effort]
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time


class CodexAppServer:
    def __init__(self, cwd=None, model=None, effort="low"):
        self.cwd = os.path.abspath(cwd) if cwd else os.getcwd()
        self.model = model
        self.effort = effort
        cmd = ["codex", "app-server"]
        # codex.exe is a real binary on this box, but shell=True keeps parity
        # with bench.py and tolerates a future .cmd shim.
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            shell=True,
        )
        self.next_id = 1
        self.pending = {}
        self.notif_q = None          # active turn's notification sink
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        self._initialize()

    def _write(self, msg):
        with self.lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _request(self, method, params, timeout=60):
        mid = self.next_id
        self.next_id += 1
        q = queue.Queue(maxsize=1)
        self.pending[mid] = q
        self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        resp = q.get(timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

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
                if "id" in msg and ("result" in msg or "error" in msg):
                    q = self.pending.pop(msg["id"], None)
                    if q:
                        q.put(msg)
                elif "id" in msg and "method" in msg:
                    # Server-initiated request (e.g. approval). Decline so we
                    # never block; a plain "Reply OK" turn shouldn't trigger one.
                    self._write({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32601, "message": "unsupported"}})
                elif "method" in msg:
                    if self.notif_q is not None:
                        self.notif_q.put((msg["method"], msg.get("params", {})))
        except Exception:
            pass

    def _stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line and line.strip():
                    print(f"[stderr] {line.rstrip()[:200]}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _initialize(self):
        self._request("initialize", {
            "clientInfo": {"name": "codex-probe", "version": "0.1"},
        }, timeout=30)
        # MCP-style lifecycle: announce we're ready. Harmless if ignored.
        try:
            self._notify("initialized", {})
        except Exception:
            pass

    def new_session(self):
        # model/effort are turn-level overrides (see TurnStartParams); thread/start
        # only carries session-scoped policy.
        params = {"approvalPolicy": "never", "sandbox": "read-only"}
        r = self._request("thread/start", params, timeout=30)
        self.thread_id = r["thread"]["id"]
        self.resolved_model = r.get("model")
        self.resolved_effort = r.get("reasoningEffort")
        return self.thread_id

    def prompt_timed(self, text, timeout=180):
        """Returns (ttfb_s, total_s, first_chunk_text, full_text)."""
        q = queue.Queue()
        self.notif_q = q
        t0 = time.monotonic()
        ttfb = None
        full = []
        first_chunk = None
        try:
            # turn/start's response only acknowledges; the answer streams as
            # notifications, terminating with turn/completed.
            mid = self.next_id
            self.next_id += 1
            pend = queue.Queue(maxsize=1)
            self.pending[mid] = pend
            tparams = {"threadId": self.thread_id,
                       "input": [{"type": "text", "text": text}]}
            if self.model:
                tparams["model"] = self.model
            if self.effort:
                tparams["effort"] = self.effort
            self._write({"jsonrpc": "2.0", "id": mid, "method": "turn/start",
                         "params": tparams})
            while True:
                method, params = q.get(timeout=timeout)
                if not isinstance(params, dict):
                    continue
                if method == "item/agentMessage/delta":
                    t = params.get("delta")
                    if t:
                        if ttfb is None:
                            ttfb = time.monotonic() - t0
                            first_chunk = t
                        full.append(t)
                elif method == "turn/completed":
                    total = time.monotonic() - t0
                    return (ttfb, total, first_chunk, "".join(full))
        finally:
            self.notif_q = None

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


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "codex"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    # gpt-5.6-luna @ low is codex's own migration target for the deprecated
    # gpt-5.4-mini (probed 2026-07: median TTFB 6.2s). Pass "" to auto-route.
    model = sys.argv[3] if len(sys.argv) > 3 else "gpt-5.6-luna"
    effort = sys.argv[4] if len(sys.argv) > 4 else "low"
    user_prompt = "Reply with exactly: OK"

    print(f"=== {label}  model={model or '(default)'} effort={effort}  n={n} ===", flush=True)

    cx = CodexAppServer(model=model, effort=effort)
    try:
        rows = []
        for i in range(1, n + 1):
            cx.new_session()
            if i == 1:
                print(f"  resolved model={cx.resolved_model} effort={cx.resolved_effort}", flush=True)
            ttfb, total, first, full = cx.prompt_timed(user_prompt)
            preview = (full or "").strip().replace("\n", " ")[:60]
            tt = f"{ttfb:.2f}s" if ttfb is not None else "n/a"
            print(f"  call {i}: ttfb={tt} total={total:.2f}s  -> {preview!r}", flush=True)
            rows.append((ttfb, total))
        ttfbs = [r[0] for r in rows if r[0] is not None]
        totals = [r[1] for r in rows]
        if ttfbs:
            print(f"  SUMMARY {label}: ttfb median={median(ttfbs):.2f}s min={min(ttfbs):.2f}s "
                  f"max={max(ttfbs):.2f}s  total median={median(totals):.2f}s", flush=True)
        print(f"BENCH_RESULT label={label} n={len(rows)} "
              f"model={cx.resolved_model} effort={cx.resolved_effort} "
              f"ttfbs={','.join(f'{x:.3f}' for x in ttfbs)} "
              f"totals={','.join(f'{x:.3f}' for x in totals)}", flush=True)
    finally:
        cx.close()


if __name__ == "__main__":
    main()
