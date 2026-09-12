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
import base64
import json
import re
import struct
import sys
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


def _image_part(img):
    """("image", mime, b64, revised) backend tuple -> the OpenRouter-style
    `images` entry (what Open WebUI / LibreChat render for image-out models)."""
    return {"type": "image_url",
            "image_url": {"url": f"data:{img[1]};base64,{img[2]}"}}


def _sse(delta, model, done=False, reasoning=False, images=None):
    # reasoning deltas ride in "reasoning_content" (the de-facto extension
    # DeepSeek popularized); standard OpenAI clients ignore the unknown key.
    # Generated images ride in "images" (OpenRouter's convention) — never in
    # content, so a JSON-expecting enrichment client is unaffected.
    if done:
        d = {}
    elif images:
        d = {"images": images}
    elif reasoning:
        d = {"reasoning_content": delta}
    else:
        d = {"content": delta}
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": d,
            "finish_reason": "stop" if done else None,
        }],
    }
    out = f"data: {json.dumps(chunk)}\n\n"
    if done:
        out += "data: [DONE]\n\n"
    return out


# Effort vocabulary across all backends; each backend validates/no-ops what
# its runtime can't apply (claude has no knob at all). "ultra" is codex-only
# (model/list: "maximum reasoning with automatic task delegation").
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def _model_label():
    """Model actually serving turns, per the backend — truthful even when a
    pin was silently overridden (org policy) or resolved from a backend
    default (codex config.toml)."""
    if _backend is not None:
        m = _backend.current_model()
        if m:
            return m
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
    # Real list when the backend can enumerate (copilot's models.list); the
    # extra fields (price_category, name) are non-standard but OpenAI clients
    # ignore unknown keys. Falls back to the single synthetic entry.
    try:
        entries = _get_backend().list_models()
    except Exception as e:
        _log(f"WARN: model listing failed: {e}")
        entries = None
    if entries:
        data = [{
            "id": m.get("id"),
            "object": "model",
            "owned_by": owner,
            "name": m.get("name") or m.get("displayName"),
            "price_category": m.get("modelPickerPriceCategory"),
            "active": m.get("id") == _model_label(),
        } for m in entries if m.get("id")]
    else:
        data = [{
            "id": f"{_model_label()}@{BACKEND_KIND}",
            "object": "model",
            "owned_by": owner,
            "active": True,
        }]
    return jsonify({"object": "list", "data": data})


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
    req_model = body.get("model")                 # optional per-request model

    prompt_text, images = _latest_user_content(messages)
    if not prompt_text and not images:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": "no user message content"}}), 400

    try:
        backend = _get_backend()
    except Exception as e:
        _REQ_T0.pop(tid, None)
        return jsonify({"error": {"message": f"backend init failed: {e}"}}), 500

    # Per-request model/effort, OpenAI-style: request fields, never server
    # state. Validated here (read-only, against the backend's cached model
    # list); APPLIED by the backend inside its turn lock, so concurrent
    # requests with different selections cannot run on each other's model.
    # Accepts bare ids and the legacy "<id>@<backend>" form /v1/models used
    # to expose; the synthetic "<backend>-default" placeholder and omission
    # both mean "the launcher default".
    want_model = None
    if isinstance(req_model, str) and req_model:
        want = req_model.split("@", 1)[0]
        if want not in ("", f"{BACKEND_KIND}-default"):
            try:
                known = {m.get("id") for m in (backend.list_models() or [])}
            except Exception as e:
                known = None
                _log(f"WARN: cannot validate model {want!r} (list_models: {e})")
            if known and want not in known:
                _REQ_T0.pop(tid, None)
                return jsonify({"error": {
                    "message": f"model {want!r} is not available on the "
                               f"{BACKEND_KIND} backend",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }}), 404
            want_model = want

    want_effort = None
    if req_reasoning in EFFORTS:
        want_effort = req_reasoning
    elif req_reasoning:
        _log(f"WARN: ignoring unknown reasoning_effort={req_reasoning!r}")

    # New chat from UI -> need a fresh session, unless the eager startup
    # session has not been used yet (in which case reuse it and avoid waste).
    # Seeded with this request's selection where the runtime wants it at
    # session scope (copilot).
    if backend.session_id is None or (
            _is_new_chat(messages) and not backend.session_fresh):
        try:
            backend.new_session(model=want_model, effort=want_effort)
        except Exception as e:
            _REQ_T0.pop(tid, None)
            return jsonify({"error": {"message": f"new_session failed: {e}"}}), 500

    img_note = f" images={len(images)}" if images else ""
    _log(f"prompt: session={backend.session_id}{img_note} text={prompt_text[:60]!r}")

    # Attribution is per-request: the label is this request's own selection,
    # falling back to what a selection-less request runs on.
    model = want_model or _model_label()
    headers = {"X-Device": BACKEND_KIND, "X-Model": model}

    if stream:
        def generate():
            try:
                for delta in backend.prompt(prompt_text, images=images,
                                            model=want_model, effort=want_effort):
                    if isinstance(delta, tuple) and delta[0] == "image":
                        yield _sse("", model, images=[_image_part(delta)])
                    elif isinstance(delta, tuple):    # ("reasoning", text)
                        yield _sse(delta[1], model, reasoning=True)
                    else:
                        yield _sse(delta, model)
                yield _sse("", model, done=True)
            finally:
                _REQ_T0.pop(tid, None)
        return Response(generate(), mimetype="text/event-stream", headers=headers)

    try:
        # Non-streaming: reasoning tuples are dropped; answer text joins and
        # generated images collect into message.images.
        parts, imgs = [], []
        for d in backend.prompt(prompt_text, images=images,
                                model=want_model, effort=want_effort):
            if isinstance(d, str):
                parts.append(d)
            elif d[0] == "image":
                imgs.append(_image_part(d))
        full = "".join(parts)
    finally:
        _REQ_T0.pop(tid, None)
    message = {"role": "assistant", "content": full}
    if imgs:
        message["images"] = imgs
    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
    }), 200, headers


# --- Images API (codex's built-in image tool) --------------------------------
#
# OpenAI Images API shape over codex's built-in image generation. codex only
# (copilot/claude have no image tool -> 501). One image per call, returned as
# b64_json: codex pins gpt-image-2 at quality=auto / size=auto and exposes no
# knobs, so `size`/`quality` are accepted and ignored (logged) rather than
# faked. Cost lands on the ChatGPT-plan window like any turn, not on API
# billing: three probe images on 2026-09-12 moved a Plus 5h window by at most
# one integer percentage point in total. NB the per-turn credits estimate in
# the log counts tokens only (an image turn reports ~10-50 output tokens), so
# it understates image turns.

# codex's tool accepts at most 5 reference images (ImagegenArgs, tool.rs).
_MAX_EDIT_IMAGES = 5


def _size_wording(size):
    """OpenAI `size` ("1536x1024", "auto", ...) -> (aspect sentence for the
    prompt, (w, h) or None).

    codex hardcodes gpt-image-2 at size=auto, and auto reads the PROMPT: in
    six trials (`_bench/codex_image_aspect_probe.py`) explicit orientation
    words pinned the aspect every time, while the subject alone decided it
    otherwise (a standing figure -> portrait). The pixel count is not ours to
    choose — outputs are normalized to ~1.57 MP (1536x1024, 1254^2, 1672x941)
    — so this asks for the RATIO and names the orientation, and the response
    reports what actually came back."""
    if not size or str(size).lower() == "auto":
        return "", None
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*", str(size))
    if not m:
        _log(f"WARN: images ignoring unparsable size={size!r}")
        return "", None
    w, h = int(m.group(1)), int(m.group(2))
    orient = ("square" if w == h else "landscape (wider than tall)" if w > h
              else "portrait (taller than wide)")
    return (f" The image MUST be {orient}, aspect ratio {w}:{h}, "
            f"i.e. {w} pixels wide by {h} pixels tall."), (w, h)


def _png_size(b64):
    """(w, h) from a base64 PNG's IHDR, or None. Decodes only the header —
    the prose codex returns claims whatever size was asked for, the IHDR
    says what was delivered."""
    try:
        head = base64.b64decode(b64[:64])
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except Exception:
        pass
    return None


def _image_request():
    """Normalize an Images API request to (fields, images).

    OpenAI's /images/edits is multipart/form-data (`image` / `image[]` file
    parts, plus form fields); /generations is JSON. We take either encoding
    on both routes, and in JSON also accept `image` as a data: URI string or
    a list of them (the same shape chat clients already send). images is a
    list of (mime, base64) like _latest_user_content() produces."""
    images = []
    if request.content_type and request.content_type.startswith("multipart/"):
        fields = request.form.to_dict()
        for key in ("image", "image[]"):
            for f in request.files.getlist(key):
                data = f.read()
                mime = f.mimetype or "image/png"
                if not mime.startswith("image/"):
                    mime = "image/png"
                images.append((mime, base64.b64encode(data).decode("ascii")))
        for key in ("mask", "mask[]"):
            if request.files.getlist(key):
                fields["mask"] = "present"
        return fields, images
    fields = request.get_json(force=True, silent=True) or {}
    raw = fields.get("image")
    for item in ([raw] if isinstance(raw, str) else (raw or [])):
        url = item.get("image_url", {}).get("url") if isinstance(item, dict) else item
        parsed = _parse_data_uri(url or "")
        if parsed:
            images.append(parsed)
        else:
            _log(f"WARN: images/edits skipping image (not a base64 data: URI): "
                 f"{str(url)[:60]!r}")
    return fields, images


def _run_image_turn(route, wrapper):
    """Shared body of the two Images routes: validate, run one codex turn that
    calls the image tool, return the OpenAI Images response. `wrapper` is the
    instruction text prefixed to the user's prompt so the tool call is
    unambiguous for the route."""
    tid = threading.get_ident()
    _REQ_T0[tid] = _now()

    def fail(status, message, code="invalid_request_error", param=None):
        _REQ_T0.pop(tid, None)
        err = {"message": message, "type": code}
        if param:
            err["param"] = param
        return jsonify({"error": err}), status

    fields, images = _image_request()
    prompt = (fields.get("prompt") or "").strip()
    if not prompt:
        return fail(400, "prompt is required", param="prompt")
    if str(fields.get("n", 1)) != "1":
        return fail(400, "only n=1 is supported", param="n")
    if fields.get("response_format", "b64_json") != "b64_json":
        return fail(400, "only response_format=b64_json is supported",
                    param="response_format")
    if route == "edits":
        if not images:
            return fail(400, "at least one reference image is required "
                             "(multipart `image` file, or a data: URI in JSON)",
                        param="image")
        if len(images) > _MAX_EDIT_IMAGES:
            return fail(400, f"at most {_MAX_EDIT_IMAGES} reference images",
                        param="image")
        if fields.get("mask"):
            return fail(400, "mask is not supported (codex's image tool takes "
                             "whole-image references only)", param="mask")
    if BACKEND_KIND != "codex":
        return fail(501, f"image generation is not available on the "
                         f"{BACKEND_KIND} backend (codex only)",
                    code="unsupported_backend")
    for k in ("quality", "style", "background", "output_format"):
        if fields.get(k) not in (None, "auto"):
            _log(f"WARN: images/{route} ignoring {k}={fields[k]!r} "
                 f"(codex pins gpt-image-2 @ auto)")
    size_text, want_size = _size_wording(fields.get("size"))

    try:
        backend = _get_backend()
        # Every Images call gets its own throwaway thread: it never touches
        # the chat session, and a batch caller doesn't build up a context of
        # old references the tool could mistake for this call's (see
        # CodexAppServerBackend.scratch_thread).
        thread_id = backend.scratch_thread()
    except Exception as e:
        return fail(500, f"backend init failed: {e}", code="server_error")

    img_note = f" images={len(images)}" if images else ""
    _log(f"image/{route}: thread={thread_id}{img_note} prompt={prompt[:60]!r}")
    # Serialized with chat turns by the backend's turn lock. The wrapper text
    # makes the tool call unambiguous.
    # "exactly once": a wordy dimensions instruction made the model call the
    # tool twice in one turn during the aspect trials (double cost). The size
    # sentence goes INSIDE the prompt body: the wrapper says "verbatim", and
    # the model obeys — a size requirement placed in the wrapper never
    # reached the tool (both test cases came back flipped, WARN fired).
    # Same paragraph, not a trailing one: with "verbatim" in play the model
    # forwarded only the first paragraph and dropped a size paragraph after
    # a blank line (generations test, WARN fired).
    body = prompt.rstrip() + (" " + size_text.strip() if size_text else "")
    text = (f"{wrapper} Call the image generation tool exactly once, passing "
            f"the complete text below as its prompt, verbatim, including any "
            f"size requirement. Then reply with one short sentence:\n\n{body}")
    imgs, notes = [], []
    try:
        for d in backend.prompt(text, images=images, thread_id=thread_id):
            if isinstance(d, tuple) and d[0] == "image":
                imgs.append(d)
            elif isinstance(d, str) and d.lstrip().startswith("["):
                notes.append(d.strip())
    finally:
        _REQ_T0.pop(tid, None)
    if not imgs:
        return fail(502, "backend returned no image" +
                    (": " + "; ".join(notes) if notes else ""), code="server_error")
    data = []
    for img in imgs:
        entry = {"b64_json": img[2], "revised_prompt": img[3]}
        got = _png_size(img[2])
        if got:
            entry["size"] = f"{got[0]}x{got[1]}"
            if want_size and abs(got[0] / got[1] - want_size[0] / want_size[1]) > 0.05:
                _log(f"WARN: images/{route} asked {want_size[0]}x{want_size[1]}, "
                     f"got {entry['size']} (aspect not honored)")
        data.append(entry)
    return jsonify({"created": int(time.time()), "data": data}), 200, {
        "X-Device": BACKEND_KIND, "X-Model": _model_label()}


@app.route("/v1/images/generations", methods=["POST"])
def images_generations():
    """POST {"prompt": ..., "size"?: "WxH"} ->
    {"data": [{"b64_json", "revised_prompt", "size"}]}."""
    return _run_image_turn(
        "generations",
        "Generate exactly one image with the image generation tool.")


@app.route("/v1/images/edits", methods=["POST"])
def images_edits():
    """Reference-anchored generation: the attached image(s) ride the turn as
    ImageUserInput and codex's tool picks them up as recent conversation
    images (`num_last_images_to_include`; no file on disk needed — probed
    2026-09-12). Without `size` the output takes the reference's proportions
    but the prompt decides orientation (a 16:9 reference gave 941x1672 for
    "a standing figure filling the frame"); `size` pins it — see
    _size_wording."""
    return _run_image_turn(
        "edits",
        "Edit the attached image(s) with the image generation tool. Use every "
        "attached image as reference. Keep the composition and aspect ratio of "
        "the attached image unless the text below says otherwise.")


# --- Entry point --------------------------------------------------------

def _check_pinned_model(backend):
    """Exit at startup if --model names something the backend cannot run.

    Codex only rejects an unknown model at turn/start, so without this a
    `-Backend codex -Model claude-sonnet-5` launch comes up "ready" and then
    fails every request. Same check the per-request path does, applied to the
    launcher pin; backends that cannot enumerate models (claude) are skipped."""
    if not BACKEND_MODEL:
        return
    try:
        models = backend.list_models() or []
    except Exception as e:
        _log(f"WARN: cannot validate --model {BACKEND_MODEL!r} (list_models: {e})")
        return
    if not models or BACKEND_MODEL in {m.get("id") for m in models}:
        return
    print(f"  ERROR: model {BACKEND_MODEL!r} is not available on the {BACKEND_KIND} "
          f"backend. Available:", flush=True)
    for m in models:
        efforts = [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []
                   if isinstance(e, dict)]
        tail = f"  ({'/'.join(efforts)})" if efforts else ""
        mark = "  [default]" if m.get("isDefault") else ""
        print(f"    {m.get('id')}{mark}{tail}", flush=True)
    sys.exit(2)


def main():
    global BACKEND_KIND, BACKEND_MODEL, REASONING_EFFORT
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--backend", choices=["copilot", "codex", "claude"], default="copilot",
                   help="Agent backend: copilot (AI-credit metered, cheapest), "
                        "codex (paid-cheap), or claude (premium, cold-start).")
    p.add_argument("--model", default=None,
                   help="Model override. copilot: e.g. gpt-5.6-luna. "
                        "codex: e.g. gpt-5.6-luna (default: codex's own "
                        "configured model, i.e. the last TUI selection). "
                        "claude: e.g. claude-sonnet-4-6 (default).")
    p.add_argument("--reasoning-effort",
                   choices=["none", "minimal", "low", "medium", "high", "xhigh",
                            "max", "ultra"],
                   default=None,
                   help="Reasoning effort override. The gpt-5.6 models take "
                        "none..max on copilot and none..ultra on codex; a "
                        "model that rejects a level keeps its previous one "
                        "(logged). No-op on claude.")
    args = p.parse_args()
    BACKEND_KIND = args.backend
    BACKEND_MODEL = args.model
    REASONING_EFFORT = args.reasoning_effort

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    LOG_DIR.mkdir(exist_ok=True)

    # Idle heartbeat: pulses '...*...*...*' in place once a second, and drops a
    # permanent quota snapshot into scrollback every 10 min (codex: rate-limit
    # %, copilot: AI-credit spend). While a turn is in flight the
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
        _check_pinned_model(backend)
        backend.new_session()
        user_note = f"user={backend.auth_login}  " if backend.auth_login else ""
        print(f"  {BACKEND_KIND} ready  ({user_note}session={backend.session_id})", flush=True)
    except Exception as e:
        print(f"  WARN: backend eager init failed: {e}", flush=True)

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
