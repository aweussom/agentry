"""Probe codex app-server's built-in image generation (codex-cli >= 0.149).

Starts app-server the way CodexAppServerBackend does (empty scratch cwd,
approvalPolicy=never, sandbox=read-only), asks for one image, and prints every
notification method seen, the full `imageGeneration` item (result truncated),
where the file landed, the token usage codex reports for the turn, and the
rate-limit snapshot before/after so the plan cost of one image is visible.

Usage: python _bench/codex_imagegen_probe.py [chat-only|allow|edit] [effort] [ref.png]
  chat-only : the backend's current CHAT_ONLY_INSTRUCTIONS (expect a refusal)
  allow     : same instructions with an image-generation carve-out
  edit      : attach ref.png as an ImageUserInput and ask for an edit of it —
              does the tool fire with a conversation image as reference (no
              local path), and does the output keep the reference's aspect?
"""
import base64
import struct
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CHAT_ONLY = (
    "You are a stateless question-answering assistant exposed over an HTTP chat "
    "API. Answer each user message directly and completely using only your own "
    "knowledge and the content of the message itself. Do not use any tools. Do "
    "not run shell commands. Do not read, list, search, or otherwise inspect "
    "files or directories. There is no relevant codebase, repository, or "
    "workspace — ignore the working directory entirely. If the message asks for "
    "a specific output format (e.g. a JSON object), return exactly that and "
    "nothing else."
)
ALLOW = CHAT_ONLY.replace(
    "Do not use any tools.",
    "The ONLY tool you may use is image generation, and only when the user "
    "explicitly asks for an image; never use any other tool.")

PROMPT = ("Generate an image: a simple flat red circle centered on a plain "
          "white background, no text. Then reply with one sentence.")
EDIT_PROMPT = ("Edit the attached image: recolor the shape to blue. Keep the "
               "composition, background, dimensions and aspect ratio exactly as "
               "in the attached image. Then reply with one sentence.")


PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def png_dims(b):
    return struct.unpack(">II", b[16:24]) if b[:8] == PNG_MAGIC else None


class Codex:
    def __init__(self, cwd, dev):
        os.makedirs(cwd, exist_ok=True)
        self.cwd = cwd
        cmd = [shutil.which("codex") or "codex", "app-server"]
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, encoding="utf-8", errors="replace")
        self.nid = 1
        self.pending = {}
        self.notifs = queue.Queue()
        self.dev = dev
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()
        self._req("initialize", {"clientInfo": {"name": "imagegen-probe", "version": "0.1"}})

    def _err(self):
        for line in iter(self.proc.stderr.readline, ""):
            print("  [stderr]", line.rstrip()[:200])

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
                print(f"  [server request] {m['method']} -> denied")
                self._send({"jsonrpc": "2.0", "id": m["id"],
                            "error": {"code": -32601, "message": "no"}})
            elif "method" in m:
                self.notifs.put((m["method"], m.get("params") or {}))

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _req(self, method, params, timeout=60):
        i = self.nid
        self.nid += 1
        q = queue.Queue(maxsize=1)
        self.pending[i] = q
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def rate_limits(self):
        try:
            return self._req("account/rateLimits/read", {}, timeout=15).get("rateLimits")
        except Exception as e:
            return f"unavailable: {e}"

    def run(self, prompt, effort, timeout=300, ref_png=None):
        params = {"approvalPolicy": "never", "sandbox": "read-only", "cwd": self.cwd,
                  "config": {"model_reasoning_summary": "detailed"}}
        if self.dev:
            params["developerInstructions"] = self.dev
        r = self._req("thread/start", params)
        tid = r["thread"]["id"]
        print(f"  thread {tid} model={r.get('model')!r}")
        while not self.notifs.empty():
            self.notifs.get_nowait()
        t0 = time.time()
        inp = [{"type": "text", "text": prompt}]
        if ref_png:
            b = open(ref_png, "rb").read()
            print(f"  reference: {os.path.basename(ref_png)} {png_dims(b)} {len(b)//1024} KB")
            inp.append({"type": "image",
                        "url": "data:image/png;base64," + base64.b64encode(b).decode()})
        r = self._req("turn/start", {"threadId": tid, "input": inp, "effort": effort})
        text, methods, images, usage = [], {}, [], None
        while True:
            method, p = self.notifs.get(timeout=timeout)
            methods[method] = methods.get(method, 0) + 1
            if method == "item/agentMessage/delta":
                text.append(p.get("delta", ""))
            elif method in ("item/started", "item/completed"):
                it = p.get("item") or {}
                if it.get("type") in ("imageGeneration", "imageView"):
                    show = dict(it)
                    res = show.get("result")
                    if isinstance(res, str) and len(res) > 120:
                        show["result"] = f"<{len(res)} chars> {res[:80]}..."
                    print(f"  {method} +{time.time()-t0:5.1f}s {json.dumps(show, ensure_ascii=False)}")
                    if method == "item/completed" and it.get("type") == "imageGeneration":
                        images.append(it)
                elif it.get("type") not in ("agentMessage", "reasoning"):
                    print(f"  {method} +{time.time()-t0:5.1f}s type={it.get('type')}")
            elif method == "thread/tokenUsage/updated":
                usage = (p.get("tokenUsage") or {}).get("last")
            elif method == "turn/completed":
                turn = p.get("turn") or {}
                print(f"  turn/completed status={turn.get('status')} "
                      f"+{time.time()-t0:5.1f}s error={turn.get('error')}")
                break
        print(f"  notifications: {methods}")
        print(f"  tokenUsage.last: {usage}")
        print(f"  assistant text: {''.join(text)[:300]!r}")
        return images

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "allow"
    effort = sys.argv[2] if len(sys.argv) > 2 else "low"
    ref = sys.argv[3] if len(sys.argv) > 3 else None
    scratch = os.path.join(tempfile.gettempdir(), "agentry-codex-imagegen-probe")
    dev = CHAT_ONLY if mode == "chat-only" else ALLOW
    prompt = EDIT_PROMPT if mode == "edit" else PROMPT
    print(f"=== mode={mode} effort={effort} cwd={scratch}")
    before = set(os.listdir(scratch)) if os.path.isdir(scratch) else set()
    cx = Codex(scratch, dev)
    try:
        rl0 = cx.rate_limits()
        print(f"  rateLimits before: {json.dumps(rl0)[:400]}")
        images = cx.run(prompt, effort, ref_png=ref if mode == "edit" else None)
        rl1 = cx.rate_limits()
        print(f"  rateLimits after : {json.dumps(rl1)[:400]}")
        for it in images:
            res = it.get("result") or ""
            sp = it.get("savedPath")
            print(f"  image: status={it.get('status')} savedPath={sp!r} "
                  f"result_len={len(res)} failure={it.get('failure')}")
            if sp and os.path.exists(sp):
                print(f"    savedPath exists, {os.path.getsize(sp)} bytes")
            try:
                print(f"    output dims: {png_dims(base64.b64decode(res))}")
            except Exception as e:
                print(f"    output not decodable: {e}")
        after = set(os.listdir(scratch)) if os.path.isdir(scratch) else set()
        print(f"  new files in cwd: {sorted(after - before)}")
    finally:
        cx.close()


if __name__ == "__main__":
    main()
