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
  ClaudeCodeBackend    Anthropic Claude Code (`claude -p`). The premium tier.
                       Unlike the other two, claude-code has NO persistent
                       stdio server mode, so this backend is COLD-START: one
                       fresh `claude -p` process per turn. Measured ~2.5s
                       startup overhead (Sonnet 4.6, lean config) — see
                       archive/CLAUDE-PLAN.md and _bench/claude_probe.py.

The transports are deliberately NOT merged into a shared base: the Copilot
path is in production, so it is kept byte-for-byte to carry zero regression
risk. The duplicated JSON-RPC / process plumbing is the price of that
isolation. See archive/CODEX-PLAN.md and archive/CLAUDE-PLAN.md.
"""

import abc
import datetime
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
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

    def quota_status(self) -> Optional[str]:
        """Short human-readable quota/usage string for the console, or None when
        the backend doesn't meter usage (e.g. an unmetered tier). Default: None;
        metered backends override."""
        return None


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

    # Monthly premium-request allotments per plan tier (GitHub Copilot).
    PLAN_LIMITS = {"free": 50, "pro": 300, "pro_plus": 1500, "pro+": 1500,
                   "business": 300, "enterprise": 1000}
    _QUOTA_TTL = 600.0   # seconds between billing-API fetches

    def __init__(self, copilot_path="copilot", cwd=None, model=None,
                 reasoning_effort=None, log_path=None, quota_config=None):
        # quota_config: optional dict from agentry.ini [copilot_quota] enabling
        # premium-request quota display via the GitHub billing API.
        self._quota = quota_config
        self._quota_cache = None
        self._quota_cache_t = 0.0
        self._quota_disabled = False
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

    def prompt(self, text, timeout=900):
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

    # --- premium-request quota (opt-in via agentry.ini) ------------------

    def _plan_limit(self):
        ml = (self._quota.get("monthly_limit") or "").strip()
        if ml.isdigit():
            return int(ml)
        return self.PLAN_LIMITS.get(self._quota.get("plan", "pro"), 300)

    @staticmethod
    def _days_to_month_reset():
        now = datetime.datetime.now(datetime.timezone.utc)
        nxt = (now.replace(year=now.year + 1, month=1, day=1)
               if now.month == 12 else now.replace(month=now.month + 1, day=1))
        # Calendar-day difference, so the last day of a month reads "1d", not "0d".
        return (nxt.date() - now.date()).days

    def _expiry_warning(self, threshold_days=14):
        exp = self._quota.get("expiry")
        if not exp:
            return None
        try:
            e = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
            days = (e - datetime.datetime.now(datetime.timezone.utc)).days
        except Exception:
            return None
        if days <= threshold_days:
            name = self._quota.get("pat_name") or "PAT"
            return f"(!) PAT '{name}' expires in {days}d"
        return None

    def _fetch_premium_used(self):
        """Sum this month's Copilot premium requests via the GitHub billing API."""
        now = datetime.datetime.now(datetime.timezone.utc)
        url = (f"https://api.github.com/users/{self._quota['username']}"
               f"/settings/billing/premium_request/usage"
               f"?year={now.year}&month={now.month}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._quota['pat']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentry",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        return sum(it.get("grossQuantity", 0) for it in data.get("usageItems", [])
                   if it.get("product") == "Copilot")

    def quota_status(self):
        """premium-request usage from the GitHub billing API (10-min cached),
        plus a PAT-expiry warning. None unless configured in agentry.ini, and
        silently disabled if the account's billing isn't exposed to the
        user-level API (e.g. an org/enterprise-managed / SSO license)."""
        if not self._quota or self._quota_disabled:
            return None
        now = time.monotonic()
        if self._quota_cache is not None and (now - self._quota_cache_t) < self._QUOTA_TTL:
            return self._quota_cache
        try:
            used = self._fetch_premium_used()
        except urllib.error.HTTPError as e:
            # 400 "Unable to get billing usage data" / 403 "No access" mean this
            # account's Copilot license is org/enterprise-managed (billing lives
            # at the enterprise level, not the user API). Disable quietly — one
            # log line, no per-tick console spam.
            if e.code in (400, 403):
                self._quota_disabled = True
                _log(f"copilot quota disabled (HTTP {e.code}): Copilot billing is "
                     f"org/enterprise-managed for this account, or the PAT lacks "
                     f"'Plan: read-only'. Not retrying.")
                return None
            self._quota_cache = f"copilot quota: HTTP {e.code}"
            self._quota_cache_t = now
            return self._quota_cache
        except Exception as e:
            self._quota_cache = f"copilot quota: unavailable ({e.__class__.__name__})"
            self._quota_cache_t = now
            return self._quota_cache
        limit = self._plan_limit()
        left = max(0, limit - used)
        pct = round(100 * left / limit) if limit else 0
        s = (f"copilot {self._quota.get('plan', 'pro')} | premium {used:g}/{limit} "
             f"({pct}% left, resets in {self._days_to_month_reset()}d)")
        warn = self._expiry_warning()
        if warn:
            s += f" | {warn}"
        self._quota_cache = s
        self._quota_cache_t = now
        return s


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
        self._rate_limits = None       # latest RateLimitSnapshot from notifications
        self._rl_lock = threading.Lock()
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
                    params = msg.get("params") or {}
                    # codex pushes account rate-limit snapshots as notifications
                    # (account/rateLimitsUpdated etc.). Cache the latest so
                    # quota_status() can render it with no extra traffic.
                    rl = params.get("rateLimits") if isinstance(params, dict) else None
                    if rl:
                        with self._rl_lock:
                            self._rate_limits = rl
                    # Notification: route to the active turn if one is listening.
                    if self.active_turn_queue is not None:
                        self.active_turn_queue.put((msg["method"], params))
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
        # Prime the quota snapshot so the console shows it immediately, without
        # waiting for the first turn's rateLimits notification.
        try:
            rl = (self._request("account/rateLimits/read", {}, timeout=10)
                  or {}).get("rateLimits")
            if rl:
                with self._rl_lock:
                    self._rate_limits = rl
                _log(f"codex quota: {self.quota_brief()}")
        except Exception as e:
            _log(f"codex quota prime skipped: {e}")

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

    def prompt(self, text, timeout=900):
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
                        status = turn.get("status")
                        if status == "failed":
                            err = (turn.get("error") or {}).get("message", "unknown")
                            yield f"\n[codex error] {err}"
                        brief = self.quota_brief()
                        _log(f"turn status={status}" + (f"  quota: {brief}" if brief else ""))
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

    @staticmethod
    def _window_label(mins):
        if not mins:
            return "window"
        if mins % 10080 == 0:
            return "weekly" if mins == 10080 else f"{mins // 10080}w"
        if mins % 1440 == 0:
            return f"{mins // 1440}d"
        if mins % 60 == 0:
            return f"{mins // 60}h"
        return f"{mins}m"

    @staticmethod
    def _fmt_reset(ts):
        if not ts:
            return None
        try:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%d %b %H:%M")
        except Exception:
            return None

    def quota_status(self):
        """Render the latest rate-limit snapshot codex pushed, e.g.
        'codex go quota | 5h 88% left (resets 31 May 12:30) | weekly 24% left
        (resets 06 Jun 10:51)'. None until the first turn populates it."""
        with self._rl_lock:
            rl = self._rate_limits
        if not isinstance(rl, dict):
            return None
        parts = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if not isinstance(w, dict) or w.get("usedPercent") is None:
                continue
            left = max(0, 100 - int(w["usedPercent"]))
            seg = f"{self._window_label(w.get('windowDurationMins'))} {left}% left"
            resets = self._fmt_reset(w.get("resetsAt"))
            if resets:
                seg += f" (resets {resets})"
            parts.append(seg)
        if not parts:
            return None
        plan = rl.get("planType") or "codex"
        return f"codex {plan} quota | " + " | ".join(parts)

    def quota_brief(self):
        """Compact form for the per-turn log line: the most-constraining window,
        e.g. 'weekly 22% left'. None until a snapshot arrives."""
        with self._rl_lock:
            rl = self._rate_limits
        if not isinstance(rl, dict):
            return None
        best = None  # (left_percent, label)
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if not isinstance(w, dict) or w.get("usedPercent") is None:
                continue
            left = max(0, 100 - int(w["usedPercent"]))
            if best is None or left < best[0]:
                best = (left, self._window_label(w.get("windowDurationMins")))
        return f"{best[1]} {best[0]}% left" if best else None

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


# --- Claude Code backend (cold-start) -----------------------------------

class ClaudeCodeBackend(Backend):
    """Cold-start client for Anthropic's Claude Code CLI (`claude -p`).

    Unlike the Copilot (`--acp`) and Codex (`app-server`) backends, claude-code
    exposes NO persistent JSON-RPC server over stdio. Its `-p` (print) mode runs
    one request and exits. So this backend spawns a FRESH `claude -p` process for
    every turn — there is no long-lived subprocess to reuse.

    Why cold-start is acceptable here (measured 2026-05-31, _bench/claude_probe.py):
    a trivial turn costs ~2.5s of startup overhead on Sonnet 4.6 with the lean
    config below — small against the 40-90s enrichment turns this is built for,
    and well inside the 5-10s budget. The win cold-start gives for free is
    ISOLATION: every turn is a brand-new conversation with zero context bleed
    from prior turns. A persistent mode would amortize startup to ~1.3s/turn but
    share one conversation across turns, reintroducing the leakage problem.
    See archive/CLAUDE-PLAN.md for the persistent-mode option.

    "Session" here is a local bookkeeping id only (the HTTP layer reads
    session_id/session_fresh); it does NOT map to any server-side claude session,
    because each prompt() is independent. The conversation history the OpenAI
    client sends is therefore NOT carried across turns — agentry already forwards
    only the latest user message, which suits the single-shot enrichment use case.

    Lean config: `claude` otherwise loads every configured MCP server (Atlassian,
    chrome-devtools, ...) and the full tool set at startup — pure overhead and a
    privacy risk for a chat-only relay. --strict-mcp-config (with no --mcp-config)
    loads zero MCP servers; --disallowedTools forbids the agentic tools; and an
    empty scratch cwd keeps agentry's own source/CLAUDE.md out of reach (the same
    defense-in-depth the codex backend uses).

    Auth: `claude` must already be logged in (the CLI's own OAuth / API key);
    `-p` runs headless and will not prompt.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"

    # Turn claude into a stateless chat answerer: no MCP servers, no agentic
    # tools. --strict-mcp-config alone (no inline --mcp-config JSON, which a
    # Windows shell would mangle) loads zero servers.
    LEAN_FLAGS = ["--strict-mcp-config",
                  "--disallowedTools",
                  "Task,Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit"]

    def __init__(self, claude_path="claude", cwd=None, model=None,
                 reasoning_effort=None, log_path=None):
        self.claude_path = claude_path
        self.model = model or self.DEFAULT_MODEL
        # claude-code (-p) has no reasoning-effort flag; we store the intent for
        # parity with the other backends but it is a no-op on the wire.
        self.reasoning_effort = reasoning_effort
        # Run claude in a dedicated EMPTY scratch dir, NEVER the agentry repo, so
        # there is nothing to find even if a tool slipped through, and agentry's
        # own CLAUDE.md / settings are not auto-loaded. Mirrors the codex backend.
        if cwd:
            self.cwd = os.path.abspath(cwd)
        else:
            self.cwd = os.path.join(tempfile.gettempdir(), "agentry-claude-scratch")
        os.makedirs(self.cwd, exist_ok=True)
        self.session_id = None
        self.session_fresh = False
        self.turn_lock = threading.Lock()
        self._proc = None                 # in-flight cold-start process, for cancel()
        self._proc_lock = threading.Lock()
        self._rate_limit = None           # latest rate_limit_event payload (quota)
        self._rl_lock = threading.Lock()
        self.log_path = log_path
        self._logf = None
        if self.log_path:
            self.log_path.parent.mkdir(exist_ok=True)
            self._logf = open(self.log_path, "w", encoding="utf-8")

    def _log_wire(self, direction, msg):
        if self._logf:
            try:
                self._logf.write(f"{direction} {json.dumps(msg)}\n")
                self._logf.flush()
            except Exception:
                pass

    def new_session(self, cwd=None):
        # No server-side session exists for cold-start; mint a local id so the
        # HTTP layer's session bookkeeping (session_id/session_fresh) works.
        if cwd:
            self.cwd = os.path.abspath(cwd)
            os.makedirs(self.cwd, exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self.session_fresh = True
        _log(f"claude session (cold-start, local id): {self.session_id}")
        return self.session_id

    def update_reasoning_effort(self, value):
        """claude-code (-p) exposes no reasoning-effort knob — thinking is model-
        and prompt-driven, not a CLI flag. Store the intent for future use but
        report no change, since nothing is applied on the wire."""
        if value != self.reasoning_effort:
            self.reasoning_effort = value
            _log(f"claude: reasoning_effort={value!r} stored but not applied (no CLI knob)")
        return False

    def _drain_stderr(self, proc):
        try:
            for line in iter(proc.stderr.readline, ""):
                if line and line.strip():
                    _log(f"claude stderr: {line.rstrip()[:200]}")
        except Exception:
            pass

    def prompt(self, text, timeout=900):
        """Generator yielding text deltas for one turn. Spawns a fresh `claude -p`
        process, feeds the prompt on stdin (robust for large enrichment prompts —
        no cmdline-length limit), and streams stream-json output back."""
        if not self.session_id:
            raise BackendError("no active session (call new_session first)")
        with self.turn_lock:
            cmd = [self.claude_path, "-p",
                   "--output-format", "stream-json",
                   "--include-partial-messages",   # emit content_block_delta for streaming
                   "--verbose",                     # required with stream-json output
                   "--model", self.model, *self.LEAN_FLAGS]
            _log(f"claude spawn: {' '.join(cmd)}  (cwd={self.cwd})")
            # claude is a real claude.exe (not a .cmd shim), so NO shell wrapper:
            # cmd.exe wrapping mangles the stdout pipe (verified). This differs
            # from the bench scripts' shell=True, which is needed for copilot.
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                encoding="utf-8", errors="replace", cwd=self.cwd,
            )
            with self._proc_lock:
                self._proc = proc
            self.session_fresh = False

            q = queue.Queue()
            def _reader():
                try:
                    for line in iter(proc.stdout.readline, ""):
                        q.put(line)
                finally:
                    q.put(None)   # EOF sentinel
            threading.Thread(target=_reader, daemon=True).start()
            threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

            # Feed the prompt and close stdin so claude starts processing.
            try:
                proc.stdin.write(text)
                proc.stdin.close()
            except Exception as e:
                _log(f"claude stdin write failed: {e}")

            got_text = False
            try:
                while True:
                    try:
                        line = q.get(timeout=timeout)
                    except queue.Empty:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield f"\n[claude timeout after {timeout}s]"
                        return
                    if line is None:          # stdout closed without a result event
                        return
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        _log(f"claude non-JSON line: {line[:120]!r}")
                        continue
                    self._log_wire("<<", msg)
                    t = msg.get("type")
                    if t == "rate_limit_event":
                        with self._rl_lock:
                            self._rate_limit = msg.get("rate_limit_info")
                    elif t == "stream_event":
                        ev = msg.get("event") or {}
                        if ev.get("type") == "content_block_delta":
                            d = (ev.get("delta") or {}).get("text")
                            if d:
                                got_text = True
                                yield d
                    elif t == "assistant" and not got_text:
                        # Fallback when partial deltas weren't emitted: the whole
                        # assistant message. Guarded so we never double-emit text
                        # already streamed via stream_event.
                        for blk in (msg.get("message") or {}).get("content", []):
                            if blk.get("type") == "text" and blk.get("text"):
                                got_text = True
                                yield blk["text"]
                    elif t == "result":
                        if msg.get("is_error") or msg.get("subtype") not in (None, "success"):
                            err = msg.get("result") or msg.get("subtype") or "unknown"
                            if not got_text:
                                yield f"\n[claude error] {err}"
                        _log(f"turn result subtype={msg.get('subtype')!r} "
                             f"dur={msg.get('duration_ms')}ms")
                        return
            finally:
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
                try:
                    if proc.poll() is None:
                        proc.terminate()
                except Exception:
                    pass

    def cancel(self):
        with self._proc_lock:
            p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
                return True
            except Exception:
                return False
        return False

    # The claude-code-quota tool (github.com/aweussom/claude-code-quota) keeps
    # this cache fresh with the real OAuth usage %, refreshed off claude's own
    # status-line ticks — no daemon. We read it passively (no network, no dep);
    # if it's absent we fall back to the coarse per-turn rate_limit_event.
    QUOTA_CACHE = Path.home() / ".claude" / "quota-data.json"

    @staticmethod
    def _fmt_reset(ts):
        if not ts:
            return None
        try:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%d %b %H:%M")
        except Exception:
            return None

    def _quota_from_cache(self):
        """Render ~/.claude/quota-data.json (the claude-code-quota tool's output)
        as e.g. 'claude quota | 5h 54% left (resets in 32m) | weekly 74% left
        (resets in 1d15h)'. None if the cache is missing/invalid."""
        try:
            with open(self.QUOTA_CACHE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return None
        if not d.get("valid"):
            return None
        sess = d.get("quota_used_pct")
        wk = d.get("weekly_used_pct")
        parts = []
        if isinstance(sess, (int, float)):
            seg = f"5h {max(0, 100 - int(sess))}% left"
            if d.get("resets_in"):
                seg += f" (resets in {d['resets_in']})"
            parts.append(seg)
        if isinstance(wk, (int, float)):
            seg = f"weekly {max(0, 100 - int(wk))}% left"
            if d.get("weekly_resets"):
                seg += f" (resets in {d['weekly_resets']})"
            parts.append(seg)
        if not parts:
            return None
        s = "claude quota | " + " | ".join(parts)
        if d.get("stale"):
            s += " (stale)"
        return s

    def quota_status(self):
        """Prefer the claude-code-quota cache (real 5h/weekly usage %). Fall back
        to the coarse rate_limit_event claude streams each turn (status + reset
        window, no %) when the tool isn't installed. None if neither is available."""
        cached = self._quota_from_cache()
        if cached:
            return cached
        with self._rl_lock:
            rl = self._rate_limit
        if not isinstance(rl, dict):
            return None
        status = rl.get("status") or "?"
        rtype = rl.get("rateLimitType") or "window"
        s = f"claude {rtype}: {status}"
        resets = self._fmt_reset(rl.get("resetsAt"))
        if resets:
            s += f" (resets {resets})"
        if rl.get("isUsingOverage"):
            s += " | on overage"
        return s

    def is_alive(self):
        # Cold-start has no persistent process to outlive; the backend can always
        # spawn a fresh `claude -p`. Always alive so _get_backend never recreates it.
        return True

    def close(self):
        with self._proc_lock:
            p = self._proc
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
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
        import config
        return CopilotACPBackend(
            model=model,
            reasoning_effort=reasoning_effort,
            log_path=log_dir / "acp_wire.log",
            quota_config=config.copilot_quota(),
        )
    if kind == "codex":
        kw = {"log_path": log_dir / "codex_wire.log"}
        if model is not None:
            kw["model"] = model
        if reasoning_effort is not None:
            kw["reasoning_effort"] = reasoning_effort
        return CodexAppServerBackend(**kw)
    if kind == "claude":
        kw = {"log_path": log_dir / "claude_wire.log"}
        if model is not None:
            kw["model"] = model
        if reasoning_effort is not None:
            kw["reasoning_effort"] = reasoning_effort
        return ClaudeCodeBackend(**kw)
    raise BackendError(f"unknown backend {kind!r} (expected 'copilot', 'codex', or 'claude')")
