"""Pluggable chat backends for agentry.

agentry is a thin OpenAI-compatible relay; each Backend wraps one persistent
agent subprocess driven over stdio (JSON-RPC 2.0, newline-delimited) and
exposes a uniform turn interface to the Flask layer.

Backends:
  CopilotACPBackend    GitHub Copilot CLI (`copilot --acp`). The free tier.
                       Behavior unchanged from the original single-backend
                       ACPClient — this class is that code, renamed.
  CodexAppServerBackend  OpenAI Codex (`codex app-server`). The paid-cheap tier
                       (ChatGPT Go $8 / Plus $20). Validated 2026-05-30;
                       default model gpt-5.4-mini @ low effort.

The two transports are deliberately NOT merged into a shared base: the Copilot
path is in production, so it is kept byte-for-byte to carry zero regression
risk. The ~40 lines of duplicated JSON-RPC plumbing are the price of that
isolation. See CODEX-PLAN.md.
"""

import abc
import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator, Optional

from logutil import log as _log


class BackendError(RuntimeError):
    pass


class Backend(abc.ABC):
    """Uniform interface the Flask layer drives.

    Implementations own one persistent subprocess and serialize turns. They
    must maintain two attributes the HTTP layer reads directly:
      session_id    None until new_session(); the active conversation id.
      session_fresh True between new_session() and its first prompt() — lets
                    the handler reuse an unused eager-start session.
    """

    session_id: Optional[str] = None
    session_fresh: bool = False

    @abc.abstractmethod
    def new_session(self, cwd: Optional[str] = None) -> str:
        """Start a fresh conversation; returns its id."""

    @abc.abstractmethod
    def prompt(self, text: str, timeout: int = 180) -> Iterator[str]:
        """Generator yielding assistant text deltas for one turn."""

    @abc.abstractmethod
    def cancel(self) -> bool:
        """Cancel the in-flight turn, if any. Returns whether a cancel was sent."""

    @abc.abstractmethod
    def update_reasoning_effort(self, value: str) -> bool:
        """Set reasoning effort; returns True if the value actually changed."""

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """True while the underlying subprocess is running."""

    @abc.abstractmethod
    def close(self) -> None:
        """Terminate the subprocess and release resources."""


# --- Copilot ACP backend -------------------------------------------------

class CopilotACPBackend(Backend):
    """JSON-RPC 2.0 client for `copilot --acp` over stdio.

    One persistent subprocess. Turns are serialized via turn_lock — the
    protocol allows concurrent sessions but we only need one for chat.

    ACP protocol (v1) messages we use:
      client -> agent   initialize          handshake; negotiates capabilities
      client -> agent   session/new         creates session, returns sessionId
      client -> agent   session/prompt      user turn; result = stopReason
      agent  -> client  session/update      streamed deltas (sessionUpdate variants)
      client -> agent   session/cancel      cancel in-flight prompt

    We treat agent->client requests (session/request_permission, fs/*,
    terminal/*) as unsupported and reply with JSON-RPC error -32601. That keeps
    the proxy a read-only chat client: copilot will not run shell commands or
    edit files.

    Spec:        https://agentclientprotocol.com/protocol/overview
    Copilot doc: https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server

    Auth: `copilot` must already be logged in (`copilot login`).
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
        self.session_fresh = False        # True between new_session() and the first prompt()
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
            raise BackendError(f"timeout waiting for {method}")
        finally:
            self.pending.pop(msg_id, None)
        if "error" in resp:
            raise BackendError(f"{method}: {resp['error']}")
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
            "clientInfo": {"name": "agentry", "version": "0.2.0"},
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
            except BackendError as e:
                cmd = terminal_hint.get("command") or "copilot"
                cmd_args = " ".join(terminal_hint.get("args") or ["login"])
                raise BackendError(
                    f"authenticate failed ({e}). "
                    f"Run `{cmd} {cmd_args}` in a separate terminal to log in, "
                    f"then restart the proxy."
                )

    def new_session(self, cwd=None):
        cwd = cwd or os.path.dirname(os.path.abspath(__file__))
        result = self._request("session/new", {"cwd": cwd, "mcpServers": []})
        self.session_id = result["sessionId"]
        self.session_fresh = True
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

    def update_reasoning_effort(self, value):
        """Idempotent: applies via set_config_option only on change. Updates
        the stored intent so future sessions also start at this value."""
        if value == self.reasoning_effort:
            return False
        self.reasoning_effort = value
        if self.session_id:
            try:
                self.set_config_option("reasoning_effort", value)
                _log(f"ACP reasoning_effort -> {value}")
                return True
            except Exception as e:
                _log(f"WARN: update reasoning_effort failed: {e}")
        return False

    def prompt(self, text, timeout=180):
        """Generator yielding text deltas for one turn. Requires an active session."""
        if not self.session_id:
            raise BackendError("no active session (call new_session first)")
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
                self.session_fresh = False
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

    def is_alive(self):
        return self.proc.poll() is None

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


# --- Codex app-server backend -------------------------------------------

class CodexAppServerBackend(Backend):
    """JSON-RPC 2.0 client for `codex app-server` over stdio.

    Codex protocol (confirmed via `codex app-server generate-json-schema`):
      client -> server  initialize               handshake; {clientInfo}
      client -> server  thread/start             new conversation; result.thread.id
      client -> server  turn/start               user turn {threadId, input, model, effort}
      server -> client  item/agentMessage/delta  streamed assistant text (params.delta)
      server -> client  turn/completed           terminal signal (params.turn.status)
      client -> server  turn/interrupt           cancel in-flight turn

    Unlike Copilot's ACP, model and reasoning effort are TURN-level params
    (turn/start), not session config — so update_reasoning_effort() just stores
    the value and the next turn carries it. Auth is the ChatGPT account login
    (`codex login`); no OPENAI_API_KEY needed.

    Reference: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
    """

    # Reasoning levels codex accepts on turn/start (ReasoningEffort enum).
    EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

    # codex is an AGENT: on a non-trivial prompt it will try to use its shell
    # tool to explore the cwd for context (e.g. grepping for JSON field names
    # it sees in an enrichment prompt). For agentry's pure-chat use that is
    # wrong, wasteful, and a privacy risk. This developer instruction tells it
    # to behave as a stateless answerer. (Necessary but not sufficient — see
    # the empty-scratch cwd below; sandbox=read-only alone does NOT stop reads,
    # because read-only commands are auto-approved regardless of approvalPolicy.)
    CHAT_ONLY_INSTRUCTIONS = (
        "You are a stateless question-answering assistant exposed over an HTTP "
        "chat API. Answer each user message directly and completely using only "
        "your own knowledge and the content of the message itself. "
        "Do not use any tools. Do not run shell commands. Do not read, list, "
        "search, or otherwise inspect files or directories. There is no relevant "
        "codebase, repository, or workspace — ignore the working directory "
        "entirely. If the message asks for a specific output format (e.g. a JSON "
        "object), return exactly that and nothing else."
    )

    def __init__(self, codex_path="codex", cwd=None, model="gpt-5.4-mini",
                 reasoning_effort="low", developer_instructions=None, log_path=None):
        self.model = model
        # codex calls it "effort"; we keep agentry's "reasoning_effort" name on
        # the public interface for parity with the Copilot backend.
        self.reasoning_effort = reasoning_effort
        self.developer_instructions = (
            self.CHAT_ONLY_INSTRUCTIONS if developer_instructions is None
            else developer_instructions)
        # Run codex in a dedicated EMPTY scratch dir, NEVER the agentry repo:
        # an empty cwd gives the agent nothing to find if it tries to explore,
        # and keeps agentry's own source/logs/memory out of reach.
        if cwd:
            self.cwd = os.path.abspath(cwd)
        else:
            self.cwd = os.path.join(tempfile.gettempdir(), "agentry-codex-scratch")
        os.makedirs(self.cwd, exist_ok=True)
        cmd = [codex_path, "app-server"]
        _log(f"codex spawn: {' '.join(cmd)}  (cwd={self.cwd})")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            cwd=self.cwd,
        )
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending = {}              # id -> Queue (for initialize, thread/start, turn/start ack)
        self.active_turn_queue = None  # Queue for the active turn's notifications: (method, params)
        self.session_id = None         # codex thread id
        self.session_fresh = False
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
            raise BackendError(f"timeout waiting for {method}")
        finally:
            self.pending.pop(msg_id, None)
        if "error" in resp:
            raise BackendError(f"{method}: {resp['error']}")
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
                    _log(f"codex non-JSON line: {line[:120]!r}")
                    continue
                self._log_wire("<<", msg)

                if "id" in msg and ("result" in msg or "error" in msg):
                    # Response to one of our requests (incl. the turn/start ack).
                    q = self.pending.pop(msg["id"], None)
                    if q is not None:
                        q.put(msg)
                elif "id" in msg and "method" in msg:
                    # Server -> client request (e.g. approval). Minimal chat
                    # client: deny everything so we never block.
                    self._write({
                        "jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601,
                                  "message": f"method '{msg['method']}' not supported by client"},
                    })
                elif "method" in msg:
                    # Notification: route to the active turn if one is listening.
                    if self.active_turn_queue is not None:
                        self.active_turn_queue.put((msg["method"], msg.get("params", {})))
        except Exception as e:
            _log(f"codex reader exited: {e}")

    def _stderr_loop(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line:
                    _log(f"codex stderr: {line.rstrip()[:200]}")
        except Exception:
            pass

    def _initialize(self):
        result = self._request("initialize", {
            "clientInfo": {"name": "agentry", "version": "0.2.0"},
        })
        cli = result.get("userAgent") or result.get("cliVersion")
        _log(f"codex initialized; server={cli!r}")
        # MCP-style lifecycle ack. Harmless if the server ignores it.
        try:
            self._notify("initialized", {})
        except Exception:
            pass

    def new_session(self, cwd=None):
        # model/effort are turn-level overrides (TurnStartParams), so thread/start
        # only carries session-scoped policy. We pin an empty cwd and inject
        # chat-only developer instructions so codex behaves as a plain answerer
        # rather than an agent exploring the filesystem.
        params = {"approvalPolicy": "never", "sandbox": "read-only",
                  "cwd": os.path.abspath(cwd) if cwd else self.cwd}
        if self.developer_instructions:
            params["developerInstructions"] = self.developer_instructions
        result = self._request("thread/start", params)
        self.session_id = result["thread"]["id"]
        self.session_fresh = True
        _log(f"codex thread: {self.session_id} (default model={result.get('model')!r})")
        return self.session_id

    def update_reasoning_effort(self, value):
        """Codex applies effort per turn, so this just records the intent for
        the next turn/start. Returns True if the value changed."""
        if value not in self.EFFORTS:
            _log(f"WARN: ignoring unsupported codex effort={value!r}")
            return False
        if value == self.reasoning_effort:
            return False
        self.reasoning_effort = value
        _log(f"codex effort -> {value} (applies next turn)")
        return True

    def prompt(self, text, timeout=180):
        """Generator yielding text deltas for one turn. Requires an active thread."""
        if not self.session_id:
            raise BackendError("no active session (call new_session first)")
        with self.turn_lock:
            q = queue.Queue()
            self.active_turn_queue = q
            try:
                # turn/start's response only acknowledges; the answer streams as
                # notifications terminating with turn/completed.
                msg_id = self._next_id()
                ack = queue.Queue(maxsize=1)
                self.pending[msg_id] = ack
                tparams = {"threadId": self.session_id,
                           "input": [{"type": "text", "text": text}]}
                if self.model:
                    tparams["model"] = self.model
                if self.reasoning_effort:
                    tparams["effort"] = self.reasoning_effort
                self._write({"jsonrpc": "2.0", "id": msg_id,
                             "method": "turn/start", "params": tparams})
                self.session_fresh = False
                while True:
                    try:
                        method, params = q.get(timeout=timeout)
                    except queue.Empty:
                        yield f"\n[codex timeout after {timeout}s]"
                        return
                    if not isinstance(params, dict):
                        continue
                    if method == "item/agentMessage/delta":
                        t = params.get("delta")
                        if t:
                            yield t
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if turn.get("status") == "failed":
                            err = (turn.get("error") or {}).get("message", "unknown")
                            yield f"\n[codex error] {err}"
                        else:
                            _log(f"turn status={turn.get('status')}")
                        return
            finally:
                self.active_turn_queue = None
                self.pending.pop(msg_id, None)

    def cancel(self):
        if not self.session_id:
            return False
        try:
            self._notify("turn/interrupt", {"threadId": self.session_id})
            return True
        except Exception:
            return False

    def is_alive(self):
        return self.proc.poll() is None

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


# --- Factory -------------------------------------------------------------

def make_backend(kind, *, model=None, reasoning_effort=None, log_dir: Optional[Path] = None) -> Backend:
    """Construct the selected backend. `model`/`reasoning_effort` None means
    'use the backend's own default'."""
    log_dir = log_dir or (Path(__file__).parent / "logs")
    if kind == "copilot":
        return CopilotACPBackend(
            model=model,
            reasoning_effort=reasoning_effort,
            log_path=log_dir / "acp_wire.log",
        )
    if kind == "codex":
        kw = {"log_path": log_dir / "codex_wire.log"}
        if model is not None:
            kw["model"] = model
        if reasoning_effort is not None:
            kw["reasoning_effort"] = reasoning_effort
        return CodexAppServerBackend(**kw)
    raise BackendError(f"unknown backend {kind!r} (expected 'copilot' or 'codex')")
