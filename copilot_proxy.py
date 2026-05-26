"""OpenAI-compatible proxy in front of GitHub Copilot CLI's ACP server.

Spawns one persistent `copilot --acp` subprocess at startup and drives it
via JSON-RPC 2.0 over stdio. Replaces the previous `-p`-per-turn backend,
which paid ~5s of process spawn + MCP boot + shutdown on every turn.

ACP protocol (v1) messages we use:
  client -> agent   initialize          handshake; negotiates capabilities
  client -> agent   session/new         creates session, returns sessionId
  client -> agent   session/prompt      user turn; result = stopReason
  agent  -> client  session/update      streamed deltas (sessionUpdate variants)
  client -> agent   session/cancel      cancel in-flight prompt

We treat agent->client requests (session/request_permission, fs/*, terminal/*)
as unsupported and reply with JSON-RPC error -32601. That keeps the proxy a
read-only chat client: copilot will not run shell commands or edit files.

Spec:        https://agentclientprotocol.com/protocol/overview
Schema:      https://github.com/zed-industries/agent-client-protocol/blob/main/schema/schema.json
Copilot doc: https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server

Auth: copilot.exe must already be logged in. Token is read from the Windows
credential store in the launcher's logon session; child processes inherit
the same session, so the subprocess sees the token.
"""

import argparse
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, Response, jsonify, request, render_template

app = Flask(__name__)

# Set at startup from CLI flags
COPILOT_MODEL = None
REASONING_EFFORT = None
LOG_DIR = Path(__file__).parent / "logs"

_REQ_T0 = {}  # tid -> request start time


def _now():
    return time.monotonic()


def _log(msg):
    t0 = _REQ_T0.get(threading.get_ident())
    elapsed = (_now() - t0) if t0 else 0.0
    print(f"[t={elapsed:6.2f}s] {msg}", flush=True)


# --- ACP client ----------------------------------------------------------

class ACPError(RuntimeError):
    pass


class ACPClient:
    """JSON-RPC 2.0 client for `copilot --acp` over stdio.

    One persistent subprocess. Turns are serialized via turn_lock — the
    protocol allows concurrent sessions but we only need one for chat.
    """

    def __init__(self, copilot_path="copilot", cwd=None, model=None,
                 reasoning_effort=None, log_path=None):
        # reasoning_effort is intentionally NOT passed to the CLI: in --acp
        # mode the flag is silently ignored and copilot uses its stored user
        # preference instead. We apply it via session/set_config_option once
        # we have a session.
        self.reasoning_effort = reasoning_effort
        # --no-custom-instructions is intentionally NOT set: we have a
        # tailored .github/copilot-instructions.md in this directory and
        # want copilot to load it (overriding any global ~/.copilot
        # instructions that were causing <system_reminder> SQL-table
        # injections in previous tests).
        cmd = [copilot_path, "--acp", "--no-ask-user", "--no-remote"]
        if model:
            cmd += ["--model", model]
        _log(f"ACP spawn: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
        )
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending = {}                 # id -> Queue (for initialize, session/new)
        self.active_turn_queue = None     # Queue for active session/prompt; tagged ("update"|"result", payload)
        self.active_prompt_id = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.log_path = log_path
        self._logf = None
        if self.log_path:
            self.log_path.parent.mkdir(exist_ok=True)
            self._logf = open(self.log_path, "w", encoding="utf-8")

        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

        self._initialize()

    def _log_wire(self, direction, msg):
        if self._logf:
            try:
                self._logf.write(f"{direction} {json.dumps(msg)}\n")
                self._logf.flush()
            except Exception:
                pass

    def _next_id(self):
        with self.id_lock:
            i = self.next_id
            self.next_id += 1
            return i

    def _write(self, msg):
        line = json.dumps(msg) + "\n"
        self._log_wire(">>", msg)
        with self.write_lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def _request(self, method, params, timeout=60):
        msg_id = self._next_id()
        q = queue.Queue(maxsize=1)
        self.pending[msg_id] = q
        self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise ACPError(f"timeout waiting for {method}")
        finally:
            self.pending.pop(msg_id, None)
        if "error" in resp:
            raise ACPError(f"{method}: {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _reader_loop(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    _log(f"ACP non-JSON line: {line[:120]!r}")
                    continue
                self._log_wire("<<", msg)

                if "id" in msg and ("result" in msg or "error" in msg):
                    # Response to one of our requests
                    if msg["id"] == self.active_prompt_id and self.active_turn_queue is not None:
                        self.active_turn_queue.put(("result", msg))
                    else:
                        q = self.pending.pop(msg["id"], None)
                        if q is not None:
                            q.put(msg)
                elif "id" in msg and "method" in msg:
                    # Agent -> client request. Minimal chat client: deny everything.
                    self._write({
                        "jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601,
                                  "message": f"method '{msg['method']}' not supported by client"},
                    })
                elif "method" in msg:
                    if msg["method"] == "session/update" and self.active_turn_queue is not None:
                        self.active_turn_queue.put(("update", msg.get("params", {}).get("update", {})))
        except Exception as e:
            _log(f"ACP reader exited: {e}")

    def _stderr_loop(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line:
                    _log(f"ACP stderr: {line.rstrip()[:200]}")
        except Exception:
            pass

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "copilot-proxy", "version": "0.2.0"},
        })
        agent_info = result.get("agentInfo") or {}
        _log(f"ACP initialized; agent={agent_info.get('name')!r} version={agent_info.get('version')!r}")
        # If the server advertises auth methods, follow through with `authenticate`.
        # An empty list means "already authed, no action needed".
        auth_methods = result.get("authMethods") or []
        if auth_methods:
            chosen = auth_methods[0]
            method_id = chosen.get("id")
            terminal_hint = ((chosen.get("_meta") or {}).get("terminal-auth") or {})
            _log(f"ACP authenticate via {method_id!r} ({chosen.get('name')!r})")
            try:
                self._request("authenticate", {"methodId": method_id}, timeout=15)
                _log(f"ACP authenticated")
            except ACPError as e:
                cmd = terminal_hint.get("command") or "copilot"
                cmd_args = " ".join(terminal_hint.get("args") or ["login"])
                raise ACPError(
                    f"authenticate failed ({e}). "
                    f"Run `{cmd} {cmd_args}` in a separate terminal to log in, "
                    f"then restart the proxy."
                )

    def new_session(self, cwd=None):
        cwd = cwd or os.path.dirname(os.path.abspath(__file__))
        result = self._request("session/new", {"cwd": cwd, "mcpServers": []})
        self.session_id = result["sessionId"]
        _log(f"ACP session: {self.session_id}")
        # Apply per-session config overrides via the standard ACP method.
        # Schema: session/set_config_option {sessionId, configId, value}.
        if self.reasoning_effort:
            try:
                self.set_config_option("reasoning_effort", self.reasoning_effort)
                _log(f"ACP reasoning_effort -> {self.reasoning_effort}")
            except Exception as e:
                _log(f"WARN: set reasoning_effort failed: {e}")
        return self.session_id

    def set_config_option(self, config_id, value):
        return self._request("session/set_config_option", {
            "sessionId": self.session_id,
            "configId": config_id,
            "value": value,
        })

    def prompt(self, text, timeout=180):
        """Generator yielding text deltas for one turn. Requires an active session."""
        if not self.session_id:
            raise ACPError("no active session (call new_session first)")
        with self.turn_lock:
            q = queue.Queue()
            msg_id = self._next_id()
            self.active_turn_queue = q
            self.active_prompt_id = msg_id
            try:
                self._write({
                    "jsonrpc": "2.0", "id": msg_id, "method": "session/prompt",
                    "params": {
                        "sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                })
                while True:
                    try:
                        kind, payload = q.get(timeout=timeout)
                    except queue.Empty:
                        yield f"\n[ACP timeout after {timeout}s]"
                        return
                    if kind == "update":
                        for delta in self._extract_delta(payload):
                            yield delta
                    elif kind == "result":
                        if "error" in payload:
                            yield f"\n[ACP error] {payload['error'].get('message', 'unknown')}"
                        else:
                            stop = (payload.get("result") or {}).get("stopReason")
                            _log(f"turn stopReason={stop}")
                        return
            finally:
                self.active_turn_queue = None
                self.active_prompt_id = None

    @staticmethod
    def _extract_delta(update):
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind != "agent_message_chunk":
            return
        content = update.get("content") or {}
        if content.get("type") == "text":
            t = content.get("text")
            if t:
                yield t

    def cancel(self):
        if not self.session_id:
            return False
        try:
            self._notify("session/cancel", {"sessionId": self.session_id})
            return True
        except Exception:
            return False

    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
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
        if self._logf:
            try:
                self._logf.close()
            except Exception:
                pass


# --- Module-level ACP state ---------------------------------------------

_acp_lock = threading.Lock()
_acp = None


def _get_acp():
    global _acp
    with _acp_lock:
        if _acp is None or _acp.proc.poll() is not None:
            _acp = ACPClient(
                model=COPILOT_MODEL,
                reasoning_effort=REASONING_EFFORT,
                log_path=LOG_DIR / "acp_wire.log",
            )
        return _acp


@atexit.register
def _shutdown_acp():
    global _acp
    if _acp:
        _acp.close()


# --- HTTP helpers -------------------------------------------------------

def _is_new_chat(messages):
    user_msgs = sum(1 for m in messages if m.get("role") == "user")
    assistant_msgs = sum(1 for m in messages if m.get("role") == "assistant")
    return user_msgs == 1 and assistant_msgs == 0


def _latest_user_text(messages):
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            return "\n".join(parts)
    return ""


def _sse(delta, model, done=False):
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {} if done else {"content": delta},
            "finish_reason": "stop" if done else None,
        }],
    }
    out = f"data: {json.dumps(chunk)}\n\n"
    if done:
        out += "data: [DONE]\n\n"
    return out


def _model_label():
    return COPILOT_MODEL or "copilot-default"


# --- Routes -------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    state = "ready" if (_acp and _acp.proc.poll() is None) else "loading"
    return jsonify({"status": state,
                    "devices": {"copilot": {"status": state, "model": _model_label()}}})


@app.route("/v1/models")
def models():
    return jsonify({
        "object": "list",
        "data": [{
            "id": f"{_model_label()}@ACP",
            "object": "model",
            "owned_by": "github-copilot",
        }],
    })


@app.route("/v1/cancel", methods=["POST"])
def cancel():
    if _acp and _acp.cancel():
        return jsonify({"cancelled": True})
    return jsonify({"cancelled": False})


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    tid = threading.get_ident()
    _REQ_T0[tid] = _now()

    body = request.get_json(force=True) or {}
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))

    prompt_text = _latest_user_text(messages)
    if not prompt_text:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": "no user message content"}}), 400

    try:
        acp = _get_acp()
    except Exception as e:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": f"ACP init failed: {e}"}}), 500

    if _is_new_chat(messages) or acp.session_id is None:
        try:
            acp.new_session()
        except Exception as e:
            _REQ_T0.pop(tid, None)
            return jsonify({"error": {"message": f"session/new failed: {e}"}}), 500

    _log(f"prompt: session={acp.session_id} text={prompt_text[:60]!r}")

    headers = {"X-Device": "Copilot-ACP", "X-Model": _model_label()}
    model = _model_label()

    if stream:
        def generate():
            try:
                for delta in acp.prompt(prompt_text):
                    yield _sse(delta, model)
                yield _sse("", model, done=True)
            finally:
                _REQ_T0.pop(tid, None)
        return Response(generate(), mimetype="text/event-stream", headers=headers)

    try:
        full = "".join(acp.prompt(prompt_text))
    finally:
        _REQ_T0.pop(tid, None)
    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full},
            "finish_reason": "stop",
        }],
    }), 200, headers


# --- Entry point --------------------------------------------------------

def main():
    global COPILOT_MODEL, REASONING_EFFORT
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--model", default=None,
                   help="Pass-through to `copilot --model` (e.g. gpt-5-mini).")
    p.add_argument("--reasoning-effort",
                   choices=["none", "low", "medium", "high", "xhigh", "max"],
                   default=None,
                   help="Pass-through. Only low/medium/high are confirmed end-to-end.")
    args = p.parse_args()
    COPILOT_MODEL = args.model
    REASONING_EFFORT = args.reasoning_effort

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    LOG_DIR.mkdir(exist_ok=True)

    print(f"  copilot-proxy on http://localhost:{args.port}  (backend=ACP)", flush=True)
    print(f"  model={COPILOT_MODEL or '(copilot default)'}  reasoning={REASONING_EFFORT or '(copilot default)'}", flush=True)
    print(f"  wire log -> {LOG_DIR / 'acp_wire.log'}", flush=True)

    # Eagerly spawn the ACP subprocess so the first user request doesn't pay
    # the handshake/session-new cost (~2-4s typically).
    try:
        acp = _get_acp()
        acp.new_session()
        print(f"  acp ready  (session={acp.session_id})", flush=True)
    except Exception as e:
        print(f"  WARN: acp eager init failed: {e}", flush=True)

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
