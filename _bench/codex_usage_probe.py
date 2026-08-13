"""Map codex app-server usage-cost surfaces (codex-cli 0.147.0).

1. model/list, account/read, account/usage/read, account/rateLimits/read
2. Short turns on chosen models; capture EVERY notification (reasoning
   deltas, thread/tokenUsage/updated, turn/completed usage payloads,
   account/rateLimits/updated pushes)
3. rateLimits/usage AFTER, to measure the per-turn quota delta.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

MODELS = [m for m in sys.argv[1:] if not m.startswith("-")] or ["gpt-5.6-luna"]
TURNS = 2


class Codex:
    def __init__(self):
        self.cwd = os.path.join(tempfile.gettempdir(), "agentry-codex-scratch")
        os.makedirs(self.cwd, exist_ok=True)
        self.proc = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8",
            errors="replace", shell=True, cwd=self.cwd)
        self.next_id = 1
        self.pending = {}
        self.notif_q = None
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "usage-probe", "version": "0.1"}})
        try:
            self._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        except Exception:
            pass

    def _write(self, msg):
        with self.lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def request(self, method, params, timeout=60):
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
                    q = self.pending.pop(msg["id"], None)
                    if q:
                        q.put(msg)
                elif "id" in msg and "method" in msg:
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

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def show(label, obj, maxlen=3000):
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=1, default=str)[:maxlen], flush=True)


def main():
    c = Codex()
    for method, params in [
        ("model/list", {}),
        ("account/read", {}),
        ("account/usage/read", {}),
        ("account/rateLimits/read", {}),
        ("modelProvider/capabilities/read", {}),
    ]:
        try:
            show(method, c.request(method, params, timeout=30))
        except Exception as e:
            print(f"\n--- {method} --- ERROR: {e}", flush=True)

    thread = c.request("thread/start", {"approvalPolicy": "never",
                                        "sandbox": "read-only", "cwd": c.cwd},
                       timeout=30)
    tid = thread["thread"]["id"]
    print(f"\nthread {tid}: default model={thread.get('model')!r} "
          f"effort={thread.get('reasoningEffort')!r}", flush=True)

    for model in MODELS:
        for i in range(1, TURNS + 1):
            q = queue.Queue()
            c.notif_q = q
            mid = c.next_id
            c.next_id += 1
            ack = queue.Queue(maxsize=1)
            c.pending[mid] = ack
            t0 = time.monotonic()
            c._write({"jsonrpc": "2.0", "id": mid, "method": "turn/start",
                      "params": {"threadId": tid, "model": model, "effort": "low",
                                 "input": [{"type": "text",
                                            "text": "Reply with exactly: OK"}]}})
            seen = {}          # method -> count
            interesting = []   # (method, payload) for usage/reasoning/limit events
            reasoning_chars = 0
            answer = []
            ttfb = None
            while True:
                try:
                    method, params = q.get(timeout=120)
                except queue.Empty:
                    print("  TIMEOUT waiting for turn completion", flush=True)
                    break
                seen[method] = seen.get(method, 0) + 1
                if method == "item/agentMessage/delta":
                    if ttfb is None:
                        ttfb = time.monotonic() - t0
                    answer.append(params.get("delta") or "")
                elif method.startswith("item/reasoning/"):
                    reasoning_chars += len(params.get("delta") or "")
                elif method in ("thread/tokenUsage/updated",
                                "account/rateLimits/updated",
                                "turn/completed", "model/rerouted"):
                    interesting.append((method, params))
                if method == "turn/completed":
                    break
            c.notif_q = None
            total = time.monotonic() - t0
            print(f"\n=== {model} turn {i}: ttfb={ttfb or -1:.2f}s "
                  f"total={total:.2f}s reasoning_chars={reasoning_chars} "
                  f"-> {''.join(answer).strip()[:30]!r}", flush=True)
            print(f"  notifications: {json.dumps(seen)}", flush=True)
            for m, p in interesting:
                show(f"  {m}", p, maxlen=2200)

    for method in ("account/usage/read", "account/rateLimits/read"):
        try:
            show(f"AFTER {method}", c.request(method, {}, timeout=30))
        except Exception as e:
            print(f"\n--- AFTER {method} --- ERROR: {e}", flush=True)
    c.close()


main()
