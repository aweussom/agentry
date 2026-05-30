"""Prove (or refute) codex prompt caching via cache-hit metadata, not wall clock.

The wall-clock bench (codex_cache_bench.py) showed a long prefix costs no extra
TTFB, which is *consistent with* caching but not proof. This reads the actual
numbers codex reports: `thread/tokenUsage/updated` -> tokenUsage.last carries
`inputTokens` and `cachedInputTokens`. If caching is real, then for a prefix
large enough to be cache-eligible, a COLD thread (first use of that exact
prefix) reports cachedInputTokens ~= 0, and a WARM thread (same prefix again,
within the cache TTL) reports cachedInputTokens covering most of the prefix.

Sweeps a size curve to also reveal the eligibility threshold and the shape:
OpenAI's documented cache only kicks in above ~1024 prompt tokens, so the two
smallest prefixes should never cache even when warm.

Usage: python _bench/codex_cache_proof.py [cycles_per_size]
"""
import json
import queue
import subprocess
import sys
import threading
import time

SIZES = [("100B", 100), ("1KB", 1_000), ("10KB", 10_000),
         ("30KB", 30_000), ("100KB", 100_000)]


def make_prefix(nbytes):
    """Deterministic filler of exactly nbytes (identical across cycles of the
    same size, so warm cycles share the prefix; distinct across sizes)."""
    base = ("You are a benchmark target. Treat every user message as a "
            "standalone request and reply with exactly: OK. ")
    # Pad with a deterministic, low-entropy but non-degenerate body.
    s = base + "".join(f"Directive {i % 1000}: keep the prefix stable. "
                       for i in range((nbytes // 40) + 2))
    return s[:nbytes]


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
        self._req("initialize", {"clientInfo": {"name": "cache-proof", "version": "0.1"}})

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

    def cycle(self, dev_instructions, timeout=180):
        """Fresh thread + one timed turn. Returns (ttfb, inputTokens, cachedInputTokens)."""
        params = {"approvalPolicy": "never", "sandbox": "read-only",
                  "developerInstructions": dev_instructions}
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
        usage = None
        done = False
        # After turn/completed, briefly drain for a trailing token-usage notif.
        grace_until = None
        while True:
            to = 0.5 if done else timeout
            try:
                method, p = self.notifs.get(timeout=to)
            except queue.Empty:
                if done:
                    break
                raise
            if method == "item/agentMessage/delta" and p.get("delta") and ttfb is None:
                ttfb = time.monotonic() - t0
            elif method == "thread/tokenUsage/updated":
                last = (p.get("tokenUsage") or {}).get("last") or {}
                usage = (last.get("inputTokens"), last.get("cachedInputTokens"))
            elif method == "turn/completed":
                done = True
                grace_until = time.monotonic() + 1.0
            if done and grace_until and time.monotonic() >= grace_until:
                break
        self.notifs = None
        inp, cached = (usage or (None, None))
        return ttfb, inp, cached

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cx = Codex()
    print(f"{'size':>6} {'cycle':>5} {'ttfb':>7} {'inputTok':>9} {'cachedTok':>10} {'cached%':>8}")
    try:
        for label, nbytes in SIZES:
            prefix = make_prefix(nbytes)
            for c in range(1, cycles + 1):
                ttfb, inp, cached = cx.cycle(prefix)
                pct = (100.0 * cached / inp) if (inp and cached is not None) else 0.0
                state = "cold" if c == 1 else "warm"
                tt = f"{ttfb:.2f}s" if ttfb is not None else "n/a"
                print(f"{label:>6} {c:>2}{state:>4} {tt:>7} {str(inp):>9} {str(cached):>10} {pct:>7.1f}%")
    finally:
        cx.close()


if __name__ == "__main__":
    main()
