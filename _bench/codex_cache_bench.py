"""Does codex amortize a long instruction prefix via prompt caching?

Parallel to the 2026-05-28 copilot bench (short vs long
.github/copilot-instructions.md), which found NO amortization — Copilot has no
prompt caching, so a long prefix was ~2x slower every turn. Codex runs on the
OpenAI Responses API, which DOES cache long prefixes server-side, so the answer
may differ.

Method: for each prefix size, run N cycles. Each cycle starts a FRESH thread
with the prefix passed as developerInstructions, then times one turn. If
caching works, cycle 1 is a cache miss (slow) and cycles 2+ hit the warm cache
(fast). If not, all cycles are uniformly slow (the copilot result).

Usage: python _bench/codex_cache_bench.py [n]
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_instr(name):
    p = os.path.join(HERE, name, ".github", "copilot-instructions.md")
    with open(p, encoding="utf-8") as f:
        return f.read()


class Codex:
    def __init__(self):
        self.proc = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8",
            errors="replace", shell=True)
        self.nid = 1
        self.pending = {}
        self.notifs = None
        threading.Thread(target=self._read, daemon=True).start()
        self._req("initialize", {"clientInfo": {"name": "cache-bench", "version": "0.1"}})

    def _read(self):
        for line in iter(self.proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in m and ("result" in m or "error" in m):
                q = self.pending.pop(m["id"], None)
                if q:
                    q.put(m)
            elif "id" in m and "method" in m:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                    "error": {"code": -32601, "message": "no"}}) + "\n")
                self.proc.stdin.flush()
            elif "method" in m and self.notifs is not None:
                self.notifs.put((m["method"], m.get("params", {})))

    def _req(self, method, params, timeout=60):
        i = self.nid; self.nid += 1
        q = queue.Queue(maxsize=1); self.pending[i] = q
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def cycle(self, dev_instructions, timeout=120):
        """Fresh thread + one timed turn. Returns TTFB seconds."""
        params = {"approvalPolicy": "never", "sandbox": "read-only"}
        if dev_instructions:
            params["developerInstructions"] = dev_instructions
        tid = self._req("thread/start", params)["thread"]["id"]
        self.notifs = queue.Queue()
        i = self.nid; self.nid += 1
        self.pending[i] = queue.Queue(maxsize=1)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "turn/start",
            "params": {"threadId": tid, "input": [{"type": "text", "text": "Reply with exactly: OK"}],
                       "model": "gpt-5.4-mini", "effort": "low"}}) + "\n")
        self.proc.stdin.flush()
        t0 = time.monotonic()
        ttfb = None
        while True:
            method, p = self.notifs.get(timeout=timeout)
            if method == "item/agentMessage/delta" and p.get("delta") and ttfb is None:
                ttfb = time.monotonic() - t0
            elif method == "turn/completed":
                self.notifs = None
                return ttfb

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    short = load_instr("short")
    long = load_instr("long")
    print(f"prefix sizes: short={len(short)}B  long={len(long)}B   n={n}\n")

    cx = Codex()
    try:
        for label, instr in (("short", short), ("long", long)):
            ttfbs = []
            for i in range(1, n + 1):
                t = cx.cycle(instr)
                ttfbs.append(t)
                print(f"  {label} cycle {i}: ttfb={t:.2f}s")
            print(f"  SUMMARY {label}: median={median(ttfbs):.2f}s "
                  f"min={min(ttfbs):.2f}s max={max(ttfbs):.2f}s\n")
    finally:
        cx.close()


if __name__ == "__main__":
    main()
