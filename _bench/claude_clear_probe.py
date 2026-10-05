"""Does `/clear` over stream-json give a fresh conversation on ONE persistent
claude process? (The leakage objection in archive/CLAUDE-PLAN.md.)

Spawns `claude -p --input-format stream-json --output-format stream-json`
once, then: (1) plants a codeword, (2) sends `/clear`, (3) asks for the
codeword. A clean reset means turn 3 answers NONE and step 2 emits a
`conversation_reset` frame.

Result 2026-09-11 (Claude Code 2.1.268, claude-haiku-4-5): conversation_reset
frame, 125 ms, $0.00; turn 3 answered NONE. So a persistent claude backend
with per-task isolation IS possible: /clear instead of respawn (~2.5 s).

Usage: python _bench/claude_clear_probe.py [--model claude-haiku-4-5-20251001]
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

LEAN = ["--strict-mcp-config", "--disallowedTools",
        "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,Task,NotebookEdit"]


def main(model):
    claude = shutil.which("claude") or "claude"
    # Empty scratch cwd, like agentry's claude backend: no CLAUDE.md, no memory,
    # so the first turn is not spent "reviewing the project context".
    cwd = os.path.join(tempfile.gettempdir(), "agentry-claude-probe")
    os.makedirs(cwd, exist_ok=True)
    cmd = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--model", model, *LEAN]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                         cwd=cwd)
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in iter(p.stdout.readline, "")],
                     daemon=True).start()

    def send(text):
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        p.stdin.flush()

    def drain(label, until_result=True, quiet=6, hard=90):
        print(f"--- {label}  (t={time.time() - t0:.1f}s)")
        last = start = time.time()
        seen_reset = False
        while time.time() - last < quiet and time.time() - start < hard:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                continue
            last = time.time()
            try:
                d = json.loads(line)
            except ValueError:
                continue
            typ, sub = d.get("type"), d.get("subtype")
            if typ == "conversation_reset":
                seen_reset = True
                print(f"  conversation_reset  new_conversation_id={d.get('new_conversation_id')}")
            elif typ == "assistant":
                txt = "".join(c.get("text", "") for c in d["message"].get("content", [])
                              if c.get("type") == "text").strip()
                if txt:
                    print(f"  assistant: {txt[:120]!r}")
            elif typ == "result":
                print(f"  result {sub} cost=${d.get('total_cost_usd', 0):.4f} "
                      f"dur={d.get('duration_ms')}ms turns={d.get('num_turns')}")
                if until_result:
                    if d.get("is_error") or sub != "success":
                        return seen_reset, None
                    return seen_reset, str(d.get("result", ""))
        return seen_reset, None

    send("Remember the codeword PINEAPPLE. Reply with exactly: OK")
    _, planted = drain("turn 1: plant codeword")
    send("/clear")
    reset, _ = drain("/clear", until_result=False, quiet=4)
    send("What codeword did I ask you to remember earlier? Reply with just the word, or NONE.")
    _, answer = drain("turn 3: recall")
    p.stdin.close()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
    print()
    print("conversation_reset frame seen:", bool(reset))
    print("turn 3 answered:", (answer or "").strip()[:40] or "(no result)")
    clean = bool(reset) and (planted or "").strip() == "OK" and (answer or "").strip() == "NONE"
    print("VERDICT:", "clean reset — persistent claude with per-task isolation is viable"
          if clean else "context leaked or no reset frame — keep cold-start")
    if not clean:
        raise SystemExit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    main(ap.parse_args().model)
