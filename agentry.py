"""OpenAI-compatible proxy in front of a pluggable agent backend.

agentry spawns one persistent agent subprocess at startup and drives it over
JSON-RPC 2.0 (stdio), exposing an OpenAI /v1/chat/completions surface. This
replaces a `-p`-per-turn model, which paid ~5s of process spawn + boot +
shutdown on every turn.

Backends (see backends.py), selected with --backend:
  copilot  GitHub Copilot CLI (`copilot --acp`) — the free tier (default).
  codex    OpenAI Codex (`codex app-server`)    — paid-cheap (ChatGPT Go/Plus).
  claude   Anthropic Claude Code (`claude -p`)  — premium. COLD-START: one fresh
           process per turn (claude-code has no persistent stdio server). ~2.5s
           startup overhead; trades that for zero cross-turn context bleed.

Each backend resolves its own model + reasoning defaults; --model and
--reasoning-effort override them.

Auth:
  copilot  must already be logged in (`copilot login`). On Windows the token
           is in the credential store, bound to the interactive logon session.
  codex    must already be logged in (`codex login`, ChatGPT account).
  claude   must already be logged in (the Claude Code CLI's own OAuth/API key).
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
                     start_keepalive, set_status_provider, set_ticker_provider)
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
            if _backend is not None:
                _backend.close()   # reap the dead process, release its wire log
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
    user_msgs = sum(1 for m in messages
                    if isinstance(m, dict) and m.get("role") == "user")
    assistant_msgs = sum(1 for m in messages
                         if isinstance(m, dict) and m.get("role") == "assistant")
    return user_msgs == 1 and assistant_msgs == 0


def _parse_data_uri(url):
    """data:image/png;base64,... -> (mime_type, base64_data), else None."""
    if not url.startswith("data:"):
        return None
    header, sep, data = url.partition(",")
    if not sep or not header.endswith(";base64") or not data:
        return None
    mime = header[len("data:"):-len(";base64")]
    return (mime or "application/octet-stream"), data


def _latest_user_content(messages):
    """(text, images) from the most recent user message. images is a list of
    (mime_type, base64_data) parsed from OpenAI image_url parts. Only data:
    URIs are accepted; remote http(s) URLs are skipped (the proxy makes no
    outbound fetches on behalf of clients)."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content, []
        if isinstance(content, list):
            texts, images = [], []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    img = _parse_data_uri(url)
                    if img:
                        images.append(img)
                    else:
                        _log(f"WARN: skipping image_url (not a base64 data: URI): {url[:60]!r}")
            return "\n".join(t for t in texts if t), images
    return "", []


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
    owner = {"codex": "openai", "claude": "anthropic"}.get(BACKEND_KIND, "github-copilot")
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

    prompt_text, images = _latest_user_content(messages)
    if not prompt_text and not images:
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

    img_note = f" images={len(images)}" if images else ""
    _log(f"prompt: session={backend.session_id}{img_note} text={prompt_text[:60]!r}")

    headers = {"X-Device": BACKEND_KIND, "X-Model": _model_label()}
    model = _model_label()

    if stream:
        def generate():
            try:
                for delta in backend.prompt(prompt_text, images=images):
                    yield _sse(delta, model)
                yield _sse("", model, done=True)
            finally:
                _REQ_T0.pop(tid, None)
        return Response(generate(), mimetype="text/event-stream", headers=headers)

    try:
        full = "".join(backend.prompt(prompt_text, images=images))
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
    p.add_argument("--backend", choices=["copilot", "codex", "claude"], default="copilot",
                   help="Agent backend: copilot (free), codex (paid-cheap), or "
                        "claude (premium, cold-start).")
    p.add_argument("--model", default=None,
                   help="Model override. copilot: e.g. gpt-5-mini. "
                        "codex: e.g. gpt-5.4-mini (default). "
                        "claude: e.g. claude-sonnet-4-6 (default).")
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
    # unmetered so no snapshot, just the pulse). While a turn is in flight the
    # same line becomes a news ticker scrolling the model's current output
    # line (reasoning summary or response), so long turns show visible work.
    set_status_provider(lambda: _backend.quota_status() if _backend else None)
    set_ticker_provider(lambda: _backend.ticker_line() if _backend else None)
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
