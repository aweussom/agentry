"""RETIRED (2026-08-13): drives the old `copilot --acp` client, which agentry
replaced with the official github-copilot-sdk. Kept for the 2026-05 result
history only — use _bench/copilot_sdk_probe.py for current numbers (SDK
transport, models.list credit table, cache verification, per-turn credits).

Minimal ACP bench: drive `copilot --acp` directly to measure TTFB and total
time per turn for a given cwd (which determines the .github/copilot-instructions.md
the agent loads at session/new).

Usage:
    python _bench/bench.py <cwd> <label> <n>
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid


class ACP:
    def __init__(self, cwd, model="gpt-5-mini"):
        self.cwd = os.path.abspath(cwd)
        cmd = ["copilot", "--acp", "--no-ask-user", "--no-remote", "--model", model]
        # On Windows, copilot is usually a .cmd shim; let the shell resolve it.
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            shell=True,
        )
        self.next_id = 1
        self.pending = {}
        self.active_id = None
        self.active_q = None
        self.session_id = None
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
                    if msg["id"] == self.active_id and self.active_q is not None:
                        self.active_q.put(("result", msg))
                    else:
                        q = self.pending.pop(msg["id"], None)
                        if q:
                            q.put(msg)
                elif "id" in msg and "method" in msg:
                    self._write({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32601, "message": "unsupported"}})
                elif "method" in msg:
                    if msg["method"] == "session/update" and self.active_q is not None:
                        self.active_q.put(("update", msg.get("params", {}).get("update", {})))
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
        r = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "bench", "version": "0.1"},
        }, timeout=30)
        for am in r.get("authMethods") or []:
            self._request("authenticate", {"methodId": am.get("id")}, timeout=30)
            break

    def new_session(self):
        r = self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=30)
        self.session_id = r["sessionId"]
        return self.session_id

    def prompt_timed(self, text, timeout=180):
        """Returns (ttfb_s, total_s, first_chunk_text, full_text)."""
        mid = self.next_id
        self.next_id += 1
        q = queue.Queue()
        self.active_id = mid
        self.active_q = q
        t0 = time.monotonic()
        ttfb = None
        full = []
        first_chunk = None
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": "session/prompt",
                         "params": {"sessionId": self.session_id,
                                    "prompt": [{"type": "text", "text": text}]}})
            while True:
                kind, payload = q.get(timeout=timeout)
                if kind == "update":
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("sessionUpdate") != "agent_message_chunk":
                        continue
                    content = payload.get("content") or {}
                    if content.get("type") == "text":
                        t = content.get("text")
                        if t:
                            if ttfb is None:
                                ttfb = time.monotonic() - t0
                                first_chunk = t
                            full.append(t)
                elif kind == "result":
                    total = time.monotonic() - t0
                    return (ttfb, total, first_chunk, "".join(full))
        finally:
            self.active_id = None
            self.active_q = None

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
    cwd = sys.argv[1]
    label = sys.argv[2]
    n = int(sys.argv[3])
    user_prompt = "Reply with exactly: OK"

    instr_path = os.path.join(cwd, ".github", "copilot-instructions.md")
    instr_bytes = os.path.getsize(instr_path) if os.path.isfile(instr_path) else 0
    print(f"=== {label}  cwd={cwd}  instructions={instr_bytes}B  n={n} ===", flush=True)

    acp = ACP(cwd)
    try:
        rows = []
        for i in range(1, n + 1):
            acp.new_session()
            ttfb, total, first, full = acp.prompt_timed(user_prompt)
            preview = (full or "").strip().replace("\n", " ")[:60]
            print(f"  call {i}: ttfb={ttfb:.2f}s total={total:.2f}s  -> {preview!r}", flush=True)
            rows.append((ttfb, total))
        ttfbs = [r[0] for r in rows if r[0] is not None]
        totals = [r[1] for r in rows]
        print(f"  SUMMARY {label}: ttfb median={median(ttfbs):.2f}s min={min(ttfbs):.2f}s "
              f"max={max(ttfbs):.2f}s  total median={median(totals):.2f}s", flush=True)
        # Emit a machine-parseable line for the orchestrator.
        print(f"BENCH_RESULT label={label} n={len(rows)} instr_bytes={instr_bytes} "
              f"ttfbs={','.join(f'{x:.3f}' for x in ttfbs)} "
              f"totals={','.join(f'{x:.3f}' for x in totals)}", flush=True)
    finally:
        acp.close()


if __name__ == "__main__":
    main()
