"""Probe `grok agent stdio` (Agent Client Protocol over JSON-RPC on stdio).

Logs every wire frame (noise filtered unless VERBOSE=1). Rejects agent->client
requests (permission, fs/*) with -32601 so we see whether grok can be driven
as a pure chat model from ACP.

    python _bench/grok_acp_probe.py [prompt]

Env:  GROK_BIN   path to grok (default ~/.grok/bin/grok.exe)
      GROK_ARGS  extra args between `agent` and `stdio`, e.g. "--agent-profile p.md"
      MODEL      session/set_model before the prompt
      EFFORT     session/set_config_option reasoning_effort before the prompt
      CWD        session cwd (default: a fresh temp dir)
      VERBOSE=1  show _x.ai/* housekeeping notifications too
"""
import json, os, shlex, subprocess, sys, threading, time, tempfile, queue

GROK = os.environ.get("GROK_BIN", os.path.expanduser("~/.grok/bin/grok.exe"))
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Reply with the single word: pong"
VERBOSE = os.environ.get("VERBOSE") == "1"
NOISE = ("_x.ai/models/update", "_x.ai/announcements/update", "_x.ai/settings/update",
         "_x.ai/session/setup", "_x.ai/queue/changed", "_x.ai/sessions/changed",
         "_x.ai/mcp/servers_updated", "_x.ai/mcp_initialized")
cwd = os.environ.get("CWD") or tempfile.mkdtemp(prefix="agentry-grok-")
cmd = [GROK, "agent"] + shlex.split(os.environ.get("GROK_ARGS", ""), posix=False) + ["stdio"]
print("CMD", cmd)
t0 = time.time()
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
responses = {}
updates = queue.Queue()

def log(d, m):
    if not VERBOSE and m.get("method") in NOISE:
        return
    if not VERBOSE and m.get("method") == "session/update" and \
            (m.get("params") or {}).get("update", {}).get("sessionUpdate") == "available_commands_update":
        return
    s = json.dumps(m)
    print(f"[{time.time()-t0:6.2f}] {d} {s[:700]}", flush=True)

def write(m):
    log("->", m)
    p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()

def reader():
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            print("RAW", line[:300]); continue
        log("<-", m)
        if "id" in m and "method" in m:          # agent -> client request
            write({"jsonrpc": "2.0", "id": m["id"],
                   "error": {"code": -32601, "message": "agentry: client methods not supported"}})
        elif "id" in m:
            responses[m["id"]] = m
        else:
            updates.put(m)
    updates.put(None)

def stderr():
    for line in p.stderr:
        print("ERR", line.rstrip()[:400], flush=True)

nid = 0
def request(method, params, timeout=60):
    global nid
    nid += 1
    write({"jsonrpc": "2.0", "id": nid, "method": method, "params": params})
    t = time.time()
    while nid not in responses:
        if time.time() - t > timeout:
            raise SystemExit(f"timeout waiting for {method}")
        if p.poll() is not None:
            raise SystemExit(f"grok exited {p.returncode}")
        time.sleep(0.02)
    return responses[nid]

threading.Thread(target=reader, daemon=True).start()
threading.Thread(target=stderr, daemon=True).start()

request("initialize", {"protocolVersion": 1,
    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
    "clientInfo": {"name": "agentry-probe", "version": "0"}})
sess = request("session/new", {"cwd": cwd, "mcpServers": []})
sid = sess.get("result", {}).get("sessionId")
print("SESSION", sid, "cwd", cwd, f"({time.time()-t0:.2f}s)")
if os.environ.get("MODEL"):
    print("SET_MODEL", json.dumps(request("session/set_model", {"sessionId": sid, "modelId": os.environ["MODEL"]}))[:300])
if os.environ.get("EFFORT"):
    print("SET_EFFORT", json.dumps(request("session/set_config_option",
          {"sessionId": sid, "configId": "reasoning_effort", "value": os.environ["EFFORT"]}))[:500])
res = request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": PROMPT}]}, timeout=180)
print("PROMPT RESULT", json.dumps(res)[:400])
time.sleep(0.5)
text, thought, tools = [], [], []
while True:
    try:
        u = updates.get_nowait()
    except queue.Empty:
        break
    if u is None:
        break
    upd = (u.get("params") or {}).get("update") or {}
    kind = upd.get("sessionUpdate")
    c = upd.get("content") or {}
    if kind == "agent_message_chunk" and c.get("type") == "text":
        text.append(c.get("text", ""))
    elif kind == "agent_thought_chunk" and c.get("type") == "text":
        thought.append(c.get("text", ""))
    elif kind == "tool_call":
        tools.append(upd.get("title"))
print("TOOL CALLS:", tools)
print("THOUGHT:", "".join(thought)[:300])
print("ASSISTANT TEXT:", "".join(text))
request("session/close", {"sessionId": sid}, timeout=10)
p.stdin.close(); p.terminate()
try: p.wait(5)
except Exception: p.kill()
