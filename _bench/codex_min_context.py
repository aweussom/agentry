"""Is codex app-server's ~24.8k base context reducible by disabling plugins?

FINDING (2026-05-30): No. A trivial turn reports ~24,789 inputTokens, and that
number is IDENTICAL whether plugins, MCP servers, and skills are cleared or
not. `-c` plumbing is confirmed working (a `-c model=...` override does change
the thread model), so this is not a syntax artifact: the desktop plugins
(browser/documents/spreadsheets/presentations/github) and the node_repl MCP
simply are not loaded into the app-server turn path. The ~24.8k is codex's
core harness (agent system prompt + built-in tool schemas) and is fixed. It is
cached server-side (see codex_cache_proof.py), so it is latency-cheap anyway.
Conclusion: disabling plugins for agentry is a no-op; don't bother.

Usage: python _bench/codex_min_context.py
"""
import json
import queue
import subprocess
import threading
import time

# Top-level keys (simple, so -c parses them — unlike the quoted per-plugin
# `plugins."x@y".enabled=false` form, which does not).
CONFIGS = [
    ("baseline", []),
    ("plugins cleared", ['-c', 'plugins={}']),
    ("plugins+mcp+skills off", ['-c', 'plugins={}', '-c', 'mcp_servers={}',
                                '-c', 'experimental_use_skills=false']),
]


class Codex:
    def __init__(self, extra_args):
        # shell=False so -c args with @ and quotes pass through unmangled.
        self.proc = subprocess.Popen(
            ["codex", "app-server"] + extra_args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace")
        self.nid = 1
        self.pending = {}
        self.notifs = None
        self.errs = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._readerr, daemon=True).start()
        self._req("initialize", {"clientInfo": {"name": "min-ctx", "version": "0.1"}})

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

    def _readerr(self):
        for line in iter(self.proc.stderr.readline, ""):
            if line.strip():
                self.errs.append(line.strip()[:160])

    def _req(self, method, params, timeout=60):
        i = self.nid; self.nid += 1
        q = queue.Queue(maxsize=1); self.pending[i] = q
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def base_input_tokens(self, timeout=120):
        tid = self._req("thread/start", {"approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
        self.notifs = queue.Queue()
        i = self.nid; self.nid += 1
        self.pending[i] = queue.Queue(maxsize=1)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "turn/start",
            "params": {"threadId": tid, "input": [{"type": "text", "text": "Reply with exactly: OK"}],
                       "model": "gpt-5.6-luna", "effort": "low"}}) + "\n")
        self.proc.stdin.flush()
        usage = None
        done = False
        grace = None
        while True:
            try:
                method, p = self.notifs.get(timeout=0.5 if done else timeout)
            except queue.Empty:
                if done:
                    break
                raise
            if method == "thread/tokenUsage/updated":
                last = (p.get("tokenUsage") or {}).get("last") or {}
                usage = last.get("inputTokens")
            elif method == "turn/completed":
                done = True; grace = time.monotonic() + 1.0
            if done and grace and time.monotonic() >= grace:
                break
        self.notifs = None
        return usage

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    for label, args in CONFIGS:
        try:
            cx = Codex(args)
            inp = cx.base_input_tokens()
            err = f"  [stderr: {cx.errs[0]}]" if cx.errs else ""
            print(f"{label:>24}: inputTokens={inp}{err}")
            cx.close()
        except Exception as e:
            print(f"{label:>24}: ERROR {e}")


if __name__ == "__main__":
    main()
