"""OpenAI-compatible proxy in front of a pluggable agent backend.

agentry spawns one persistent agent subprocess at startup and drives it over
JSON-RPC 2.0 (stdio), exposing an OpenAI /v1/chat/completions surface. This
replaces a `-p`-per-turn model, which paid ~5s of process spawn + boot +
shutdown on every turn.

Backends (see backends.py), selected with --backend:
  copilot  GitHub Copilot CLI (`copilot --acp`) — the free tier (default).
  codex    OpenAI Codex (`codex app-server`)    — paid-cheap (ChatGPT Go/Plus).

Each backend resolves its own model + reasoning defaults; --model and
--reasoning-effort override them.

Auth:
  copilot  must already be logged in (`copilot login`). On Windows the token
           is in the credential store, bound to the interactive logon session.
  codex    must already be logged in (`codex login`, ChatGPT account).
"""

import argparse
import atexit
import json
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, Response, jsonify, request, render_template

from logutil import (REQ_T0 as _REQ_T0, now as _now, log as _log,
                     start_keepalive, set_status_provider)
from backends import make_backend

app = Flask(__name__)

# Set at startup from CLI flags
BACKEND_KIND = "copilot"
BACKEND_MODEL = None
REASONING_EFFORT = None
LOG_DIR = Path(__file__).parent / "logs"


# --- Module-level backend state -----------------------------------------

_backend_lock = threading.Lock()
_backend = None


def _get_backend():
    global _backend
    with _backend_lock:
        if _backend is None or not _backend.is_alive():
            _backend = make_backend(
                BACKEND_KIND,
                model=BACKEND_MODEL,
                reasoning_effort=REASONING_EFFORT,
                log_dir=LOG_DIR,
            )
        return _backend


@atexit.register
def _shutdown_backend():
    global _backend
    if _backend:
        _backend.close()


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
    return BACKEND_MODEL or f"{BACKEND_KIND}-default"


# --- Routes -------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    state = "ready" if (_backend and _backend.is_alive()) else "loading"
    return jsonify({"status": state,
                    "devices": {BACKEND_KIND: {"status": state, "model": _model_label()}}})


@app.route("/v1/models")
def models():
    owner = "openai" if BACKEND_KIND == "codex" else "github-copilot"
    return jsonify({
        "object": "list",
        "data": [{
            "id": f"{_model_label()}@{BACKEND_KIND}",
            "object": "model",
            "owned_by": owner,
        }],
    })


@app.route("/v1/cancel", methods=["POST"])
def cancel():
    if _backend and _backend.cancel():
        return jsonify({"cancelled": True})
    return jsonify({"cancelled": False})


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    tid = threading.get_ident()
    _REQ_T0[tid] = _now()

    body = request.get_json(force=True) or {}
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))
    req_reasoning = body.get("reasoning_effort")  # optional per-request override

    prompt_text = _latest_user_text(messages)
    if not prompt_text:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": "no user message content"}}), 400

    try:
        backend = _get_backend()
    except Exception as e:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": f"backend init failed: {e}"}}), 500

    # New chat from UI -> need a fresh session, unless the eager startup
    # session has not been used yet (in which case reuse it and avoid waste).
    if backend.session_id is None:
        try:
            backend.new_session()
        except Exception as e:
            _REQ_T0.pop(tid, None)
            return jsonify({"error": {"message": f"new_session failed: {e}"}}), 500
    elif _is_new_chat(messages) and not backend.session_fresh:
        try:
            backend.new_session()
        except Exception as e:
            _REQ_T0.pop(tid, None)
            return jsonify({"error": {"message": f"new_session failed: {e}"}}), 500

    # Guard: forward only reasoning values known to round-trip cleanly on both
    # backends. Copilot/gpt-5-mini rejects "none" ("Supported: minimal, low,
    # medium, high"); xhigh/max are CLI-only. low/medium/high are safe for both.
    if req_reasoning in ("low", "medium", "high"):
        backend.update_reasoning_effort(req_reasoning)
    elif req_reasoning:
        _log(f"WARN: ignoring unsupported reasoning_effort={req_reasoning!r}")

    _log(f"prompt: session={backend.session_id} text={prompt_text[:60]!r}")

    headers = {"X-Device": BACKEND_KIND, "X-Model": _model_label()}
    model = _model_label()

    if stream:
        def generate():
            try:
                for delta in backend.prompt(prompt_text):
                    yield _sse(delta, model)
                yield _sse("", model, done=True)
            finally:
                _REQ_T0.pop(tid, None)
        return Response(generate(), mimetype="text/event-stream", headers=headers)

    try:
        full = "".join(backend.prompt(prompt_text))
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
    global BACKEND_KIND, BACKEND_MODEL, REASONING_EFFORT
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--backend", choices=["copilot", "codex"], default="copilot",
                   help="Agent backend: copilot (free) or codex (paid-cheap).")
    p.add_argument("--model", default=None,
                   help="Model override. copilot: e.g. gpt-5-mini. "
                        "codex: e.g. gpt-5.4-mini (default).")
    p.add_argument("--reasoning-effort",
                   choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                   default=None,
                   help="Reasoning effort override. Only low/medium/high are "
                        "confirmed end-to-end on copilot.")
    args = p.parse_args()
    BACKEND_KIND = args.backend
    BACKEND_MODEL = args.model
    REASONING_EFFORT = args.reasoning_effort

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    LOG_DIR.mkdir(exist_ok=True)

    # Idle heartbeat: pulses '...*...*...*' in place once a second, and drops a
    # permanent quota snapshot into scrollback every 10 min (codex; copilot is
    # unmetered so no snapshot, just the pulse).
    set_status_provider(lambda: _backend.quota_status() if _backend else None)
    start_keepalive(pulse_interval=1.0, snapshot_interval=600.0)

    print(f"  agentry on http://localhost:{args.port}  (backend={BACKEND_KIND})", flush=True)
    print(f"  model={BACKEND_MODEL or '(backend default)'}  reasoning={REASONING_EFFORT or '(backend default)'}", flush=True)

    # Eagerly spawn the backend subprocess so the first user request doesn't
    # pay the handshake/session-new cost (~2-4s typically).
    try:
        backend = _get_backend()
        backend.new_session()
        print(f"  {BACKEND_KIND} ready  (session={backend.session_id})", flush=True)
    except Exception as e:
        print(f"  WARN: backend eager init failed: {e}", flush=True)

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
