"""Live Claude startup comparison; short subscription-billed requests.

Compares the old lean cold launch, safe-mode cold launch, and safe-mode
stream-json reuse with an acknowledged /clear before every subsequent turn.
Run: venv/Scripts/python -u _bench/claude_startup_bench.py --samples 3
No prompt/result payloads or credentials are saved.
"""
import argparse
import json
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

OLD_FLAGS = ["--strict-mcp-config", "--disallowedTools",
             "Task,Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit"]
NEW_FLAGS = ["--safe-mode", "--strict-mcp-config", "--tools", "",
             "--no-session-persistence"]


class Wire:
    def __init__(self, model, flags, cwd):
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [shutil.which("claude") or "claude", "-p", "--input-format", "stream-json",
             "--output-format", "stream-json", "--include-partial-messages",
             "--verbose", "--model", model, *flags], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        self.events = queue.Queue()
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.events.put(None)

    def _errors(self):
        for line in self.proc.stderr:
            self.stderr.append(line.strip()[:200])
            self.stderr[:] = self.stderr[-3:]

    def turn(self, text, reset=False, started=None):
        t0 = time.monotonic() if started is None else started
        self.proc.stdin.write(json.dumps({"type": "user", "message": {
            "role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()
        reset_seen, ttft, answer = False, None, []
        deadline = time.monotonic() + (10 if reset else 90)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError("Claude response deadline exceeded")
            try:
                msg = self.events.get(timeout=left)
            except queue.Empty:
                raise RuntimeError("Claude response timeout: " + " / ".join(self.stderr))
            if msg is None:
                raise RuntimeError("Claude exited before result: " + " / ".join(self.stderr))
            kind = msg.get("type")
            if kind == "conversation_reset":
                reset_seen = True
            elif kind == "stream_event":
                delta = (msg.get("event") or {}).get("delta") or {}
                if delta.get("text"):
                    ttft = ttft if ttft is not None else time.monotonic() - t0
                    answer.append(delta["text"])
            elif kind == "result":
                if msg.get("is_error") or msg.get("subtype") != "success":
                    raise RuntimeError(f"Claude failed: {msg.get('result') or msg.get('subtype')}")
                if reset and not reset_seen:
                    raise RuntimeError("/clear completed without conversation_reset")
                return {"total_s": round(time.monotonic() - t0, 3),
                        "ttft_s": round(ttft, 3) if ttft is not None else None,
                        "reset_seen": reset_seen,
                        "answer": "".join(answer) or msg.get("result", ""),
                        "usage": msg.get("usage")}

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--implementation", action="store_true",
                    help="validate the real Flask/Claude backend instead of the wire comparison")
    args = ap.parse_args()
    if args.samples < 1:
        ap.error("samples must be positive")
    print(subprocess.check_output([shutil.which("claude") or "claude", "--version"],
                                  text=True).strip(), flush=True)
    if args.implementation:
        return implementation(args.model, args.samples)
    with tempfile.TemporaryDirectory(prefix="agentry-claude-bench-") as cwd:
        for label, flags in (("old-lean-cold", OLD_FLAGS), ("safe-cold", NEW_FLAGS)):
            times = []
            for _ in range(args.samples):
                wire = Wire(args.model, flags, cwd)
                try:
                    result = wire.turn("Reply with exactly: OK", started=wire.started)
                    if result.pop("answer").strip() != "OK":
                        raise RuntimeError("unexpected benchmark answer")
                    times.append(result["total_s"])
                    print(label, json.dumps(result), flush=True)
                finally:
                    wire.close()
            print(label, "median_s", statistics.median(times), flush=True)
        wire = Wire(args.model, NEW_FLAGS, cwd)
        try:
            first = wire.turn("Remember the codeword PINEAPPLE. Reply with exactly: OK",
                              started=wire.started)
            if first.pop("answer").strip() != "OK":
                raise RuntimeError("codeword setup failed")
            print("persistent-first", json.dumps(first), flush=True)
            times = []
            for i in range(args.samples):
                reset = wire.turn("/clear", reset=True)
                text = ("What codeword did I ask you to remember earlier? "
                        "Reply with just the word, or NONE." if i == 0
                        else "Reply with exactly: OK")
                result = wire.turn(text)
                answer = result.pop("answer").strip()
                if answer != ("NONE" if i == 0 else "OK"):
                    raise RuntimeError("reset isolation or benchmark answer failed")
                result["reset_s"] = reset["total_s"]
                times.append(result["total_s"] + reset["total_s"])
                print("persistent-reset-turn", json.dumps(result), flush=True)
            print("persistent-reset-turn median_s", statistics.median(times), flush=True)
        finally:
            wire.close()


def implementation(model, samples):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import agentry
    from backends import ClaudeCodeBackend

    with tempfile.TemporaryDirectory(prefix="agentry-claude-acceptance-") as cwd:
        backend = ClaudeCodeBackend(model=model, cwd=cwd)
        agentry._backend = backend
        agentry.BACKEND_KIND = "claude"
        client = agentry.app.test_client()
        try:
            def request(text, expected, stream=False):
                t0 = time.monotonic()
                response = client.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": text}], "stream": stream})
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP failure: {response.status_code}")
                if stream:
                    chunks = response.get_data(as_text=True).splitlines()
                    answer = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                                     for line in chunks if line.startswith("data: ")
                                     and line != "data: [DONE]")
                else:
                    answer = response.get_json()["choices"][0]["message"]["content"]
                if answer.strip() != expected:
                    raise RuntimeError(f"Unexpected reply: {answer[:120]!r}")
                if not backend._reuse:
                    raise RuntimeError("Worker reuse fell back during acceptance")
                return round(time.monotonic() - t0, 3)

            first = request("Remember the codeword PINEAPPLE. Reply with exactly: OK", "OK")
            pid = backend._proc.pid
            recall = request("What codeword did I ask you to remember earlier? "
                             "Reply with just the word, or NONE.", "NONE", stream=True)
            if backend._proc.pid != pid:
                raise RuntimeError("Isolation test restarted instead of reusing the worker")
            print("implementation isolation PASS", json.dumps({"first_s": first, "recall_s": recall}),
                  flush=True)
            times = [request("Reply with exactly: OK", "OK", stream=bool(i % 2))
                     for i in range(samples)]
            if backend._proc.pid != pid:
                raise RuntimeError("Worker restarted during warm benchmark")
            print("implementation warm reset+turn", times, "median_s", statistics.median(times),
                  flush=True)
        finally:
            backend.close()
            agentry._backend = None


if __name__ == "__main__":
    main()
