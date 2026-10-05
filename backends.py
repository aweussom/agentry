"""Pluggable chat backends for agentry.

agentry is a thin OpenAI-compatible relay; each Backend wraps one persistent
agent subprocess driven through the SDK or structured stdio and
exposes a uniform turn interface to the Flask layer.

Backends:
  CopilotSDKBackend    GitHub Copilot via the official github-copilot-sdk
                       (JSON-RPC to the Copilot CLI runtime in server mode).
                       The cheapest tier (Copilot AI credits, billed per
                       token). Replaced the hand-rolled `copilot --acp`
                       client on this branch — see git history for that code.
  CodexAppServerBackend  OpenAI Codex (`codex app-server`). The paid-cheap tier
                       (ChatGPT Go $8 / Plus $20). Validated 2026-05-30;
                       inherits codex's own configured model (the one last
                       selected in the codex TUI) @ low effort.
  ClaudeCodeBackend    Anthropic Claude Code (`claude -p`, stream-json input).
                       A persistent lean worker with a confirmed /clear before
                       each subsequent request; restart on model change or
                       failure. See archive/CLAUDE-STARTUP-2026-09-23.md.
  GrokACPBackend       xAI Grok Build (`grok agent stdio`, Agent Client
                       Protocol). SuperGrok / X Premium+ subscription. One ACP
                       session per chat; tools removed by an agent profile.
                       Validated 2026-10-04; see archive/GROK-PLAN.md.

The transports are deliberately NOT merged into a shared base: each backend
owns its plumbing so a change to one carries zero regression risk for the
others. The Copilot backend delegates its transport to the official SDK; the
Codex and Claude backends keep their hand-rolled JSON-RPC / subprocess code.
See archive/CODEX-PLAN.md and archive/CLAUDE-PLAN.md.
"""

import abc
import asyncio
import base64
import datetime
import json
import logging
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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
    # GitHub/provider login the backend authenticated as, when knowable.
    # Shown in the startup ready-line so a mismatched quota display (which may
    # meter a different account) can't be misread as the session identity.
    auth_login: Optional[str] = None

    @abc.abstractmethod
    def new_session(self, cwd: Optional[str] = None,
                    model: Optional[str] = None,
                    effort: Optional[str] = None) -> str:
        """Start a fresh conversation; returns its id. model/effort seed the
        session where the runtime scopes them per session (copilot); backends
        whose runtime scopes them per turn ignore them here."""

    @abc.abstractmethod
    def prompt(self, text: str, images=None, timeout: int = 180,
               model: Optional[str] = None,
               effort: Optional[str] = None) -> Iterator[str]:
        """Generator yielding assistant text deltas for one turn.

        model/effort are this turn's selection; None means the backend's
        launcher default. They are request fields, OpenAI-style — never
        mutable server state: the backend applies them inside its turn lock,
        so concurrent requests with different selections cannot run on each
        other's model.

        images: optional list of (mime_type, base64_data) tuples to attach to
        the user turn. Backends that can't forward them must drop them loudly
        (log + a visible note in the reply), never silently.

        Backends that stream reasoning may additionally yield tagged tuples
        ("reasoning", str) interleaved with the plain-str answer deltas;
        consumers that only want the answer must filter for str items.
        Backends whose runtime can generate images yield each finished image
        as ("image", mime_type, base64_data, revised_prompt_or_None) — never
        inline in the text, so JSON-expecting clients keep a clean content.
        A finished video is ("video", mime_type, file_path, revised_prompt):
        a path, not bytes, because the Videos route serves it on a later
        GET rather than inlining a megabyte of MP4 in JSON."""

    @abc.abstractmethod
    def cancel(self) -> bool:
        """Cancel the in-flight turn, if any. Returns whether a cancel was sent."""

    def current_model(self) -> Optional[str]:
        """Model a request that omits `model` runs on, when knowable: the
        launcher default, refined by runtime truth where the backend has it.
        None means unknown (the HTTP layer then labels '<backend>-default')."""
        return getattr(self, "default_model", None)

    def list_models(self):
        """Available models as a list of dicts (at minimum {'id': ...}), or
        None when the backend cannot enumerate them."""
        return None

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

    def ticker_line(self) -> Optional[str]:
        """Latest line of in-flight turn output (reasoning or response) for the
        console's live ticker, or None when no turn is running / the backend
        doesn't surface it. Default: None; streaming backends may override."""
        return None


# --- Copilot SDK backend ---------------------------------------------------

def _relocate_runtime_for_store_python():
    """Keep the SDK's downloaded runtime out of %LOCALAPPDATA% when running on
    a Microsoft Store Python.

    Store-packaged Pythons virtualize writes under AppData: the SDK "caches"
    its runtime bundle at %LOCALAPPDATA%/github-copilot-sdk/..., but the files
    physically land in Packages/PythonSoftwareFoundation.../LocalCache. Python
    itself sees them through the redirect, and so does CreateProcess on
    copilot-runtime.exe, but the wrapper's LoadLibraryExW(runtime.node) does
    not; the CLI dies at startup with "failed to load runtime cdylib ...
    LoadLibraryExW failed". A project-local directory is not virtualized, so
    point the SDK's COPILOT_CLI_EXTRACT_DIR override there (it replaces the
    whole version-specific cache dir, hence the version suffix).

    No-op off Windows, on a regular python.org/winget install, or when the
    caller already set the override.
    """
    if sys.platform != "win32" or os.environ.get("COPILOT_CLI_EXTRACT_DIR"):
        return
    if "WindowsApps" not in sys.base_prefix:
        return
    try:
        from copilot._cli_version import CLI_VERSION
    except ImportError:
        return
    target = Path(__file__).parent / ".copilot-runtime" / (CLI_VERSION or "unpinned")
    os.environ["COPILOT_CLI_EXTRACT_DIR"] = str(target)
    _log(f"Store Python detected; Copilot runtime dir -> {target}")


class CopilotSDKBackend(Backend):
    """GitHub Copilot via the official github-copilot-sdk (Python 3.11+).

    The SDK spawns the Copilot CLI runtime in server mode and owns the wire
    protocol (JSON-RPC over stdio); this class only bridges the SDK's
    async-only API onto agentry's sync Backend interface. One asyncio event
    loop runs in a daemon thread; sync methods submit coroutines with
    run_coroutine_threadsafe and block on the Future. Turn deltas flow through
    a queue.Queue drained by the prompt() generator — the same shape the
    retired hand-rolled `copilot --acp` client used (see git history on main).

    Read-only chat client, enforced two ways:
      available_tools=[]          the session exposes no tools at all
      deny-all permission handler belt-and-braces if anything slips through

    Runtime binary: the SDK downloads and caches its own pinned CLI build on
    first start (one-time network fetch); it does NOT use the `copilot` on
    PATH. Auth is shared regardless: the runtime reads the same ~/.copilot
    credential store, so an existing `copilot login` covers it. On a
    Microsoft Store Python the cache is relocated into the repo
    (.copilot-runtime/, gitignored) — see _relocate_runtime_for_store_python.

    SDK: https://github.com/github/copilot-sdk  (pip install github-copilot-sdk)
    """

    def __init__(self, cwd=None, model=None, reasoning_effort=None,
                 log_path=None):
        try:
            from copilot import CopilotClient
            from copilot.rpc import (AccountGetQuotaRequest, ModelsListRequest,
                                     PermissionDecisionReject)
            from copilot.session_events import (
                AssistantMessageDeltaData, AssistantReasoningDeltaData,
                AssistantUsageData, SessionErrorData, SessionIdleData)
        except ImportError as e:
            raise BackendError(
                "github-copilot-sdk is not installed (pip install github-copilot-sdk; "
                "requires Python 3.11+)") from e
        # Event/decision classes are stashed on self because the SDK import is
        # deferred (other backends must not require it).
        self._DeltaData = AssistantMessageDeltaData
        self._ReasoningDeltaData = AssistantReasoningDeltaData
        self._UsageData = AssistantUsageData
        self._ErrorData = SessionErrorData
        self._IdleData = SessionIdleData
        self._Reject = PermissionDecisionReject
        self._ModelsListRequest = ModelsListRequest
        self._AccountGetQuotaRequest = AccountGetQuotaRequest
        self._CopilotClient = CopilotClient   # for throwaway quota-refresh runtimes

        # Launcher defaults — immutable after construction. Per-request
        # selection never lands here; it rides through prompt(model=, effort=).
        self.default_model = model
        self.default_effort = reasoning_effort
        self._current_model = None        # what the runtime says is active (get_current)
        self._current_effort = None       # effort the live session was last set to
        self._models_cache = None         # models.list result, fetched once on demand
        # AI-credit accounting from per-turn AssistantUsageData events
        # (copilotUsage.totalNanoAiu; 1e9 nanoAIU = 1 credit = $0.01).
        self._turn_credits = 0.0          # accumulates across a turn's model calls
        self._session_credits = 0.0       # running total since client start
        # Account-wide credit allowance (account/getQuota) as a BASELINE dict
        # {used, cap, unlimited, overage, overage_ok, fetched_at}; None until
        # the first successful fetch. Refreshed on a TTL from quota_status() by
        # a background thread. See _refresh_plan_quota / _plan_quota_line.
        self._plan_quota = None
        self._plan_quota_at = 0.0         # monotonic time of the last refresh
        self._plan_quota_busy = False     # a refresh thread is already in flight
        self._ledger_cache = None         # (since, monotonic, credits|None)
        self._cwd = cwd or os.path.dirname(os.path.abspath(__file__))
        self._session = None
        self.session_id = None
        self.session_fresh = False        # True between new_session() and the first prompt()
        self.active_turn_queue = None     # Queue for the in-flight turn; tagged ("delta"|"error"|"idle", payload)
        self.turn_lock = threading.Lock()
        self._alive = False
        self._ticker_buf = ""             # tail of the in-flight turn's streamed text
        self._ticker_kind = None          # "reasoning"|"message"; newline on phase switch
        self._turn_t0 = 0.0               # monotonic start of the in-flight turn

        # The SDK logs through the stdlib `copilot` logger hierarchy; a file
        # handler there is the closest equivalent of the old wire log.
        self._log_handler = None
        if log_path:
            log_path.parent.mkdir(exist_ok=True)
            self._log_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
            self._log_handler.setFormatter(
                logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
            sdk_logger = logging.getLogger("copilot")
            sdk_logger.setLevel(logging.DEBUG)
            sdk_logger.addHandler(self._log_handler)

        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True,
                         name="copilot-sdk-loop").start()

        # skip_custom_instructions is left at its default (off): we have a
        # tailored .github/copilot-instructions.md in this directory and want
        # the runtime to load it.
        _relocate_runtime_for_store_python()
        self._client = CopilotClient(working_directory=self._cwd)
        t0 = time.monotonic()
        _log("SDK client starting (first run downloads the pinned Copilot runtime)")
        self._call(self._client.start(), timeout=600)
        self._alive = True
        _log(f"SDK client started in {time.monotonic() - t0:.1f}s")
        try:
            st = self._call(self._client.get_auth_status(), timeout=15)
            self.auth_login = st.login
            _log(f"SDK auth: login={st.login} type={st.authType} host={st.host}")
        except Exception as e:
            _log(f"SDK auth status unavailable: {e}")
        # Prime the credit allowance now: the heartbeat prints its first status
        # line seconds from here, and the value is otherwise absent until the
        # TTL expires or the first turn runs.
        self._plan_quota_busy = True
        threading.Thread(target=self._refresh_plan_quota, args=(self._client,),
                         daemon=True, name="copilot-quota-prime").start()

    def _call(self, coro, timeout=60):
        """Run a coroutine on the SDK loop from sync code; block for the result."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise BackendError(f"timeout after {timeout}s waiting for SDK call")
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"{e.__class__.__name__}: {e}") from e

    def _deny_permission(self, request, invocation):
        # Minimal chat client: deny everything (the session also exposes no
        # tools, so this should never fire).
        return self._Reject(feedback="agentry is a read-only chat relay; "
                                     "tool use is not permitted")

    def _on_event(self, event):
        """Session event handler; runs on the SDK loop thread. Feeds the
        in-flight turn's queue, drops events between turns (e.g. the idle
        emitted right after session creation)."""
        q = self.active_turn_queue
        if q is None:
            return
        d = event.data
        if isinstance(d, self._DeltaData):
            if d.delta_content:
                self._ticker_feed("message", d.delta_content)
                q.put(("delta", d.delta_content))
        elif isinstance(d, self._ReasoningDeltaData):
            # Reasoning feeds the console ticker, streams to the web UI as
            # tagged deltas, and proves the model is working: without an item
            # on the queue, a long silent reasoning stretch (high effort)
            # would trip prompt()'s inactivity timeout mid-think.
            if d.delta_content:
                self._ticker_feed("reasoning", d.delta_content)
                q.put(("reasoning", d.delta_content))
            else:
                q.put(("keepalive", None))
        elif isinstance(d, self._UsageData):
            # Exact AI-credit cost of the model call (1e9 nanoAIU = 1 credit).
            cu = d.copilot_usage
            nano = (cu.get("totalNanoAiu") if isinstance(cu, dict)
                    else getattr(cu, "total_nano_aiu", None)) if cu else None
            if nano:
                self._turn_credits += nano / 1e9
                self._session_credits += nano / 1e9
            q.put(("keepalive", None))
        elif isinstance(d, self._ErrorData):
            q.put(("error", d.message or d.error_type or "unknown"))
        elif isinstance(d, self._IdleData):
            q.put(("idle", None))
        else:
            # Any other session event (ModelCallStart, usage ticks, ...) still
            # proves the runtime is alive — count it against the inactivity
            # timeout, render nothing.
            q.put(("keepalive", None))

    def _ticker_feed(self, kind, text):
        """Append streamed text to the ticker buffer (tail-capped); a phase
        switch (reasoning<->message) starts a fresh line."""
        if kind != self._ticker_kind:
            self._ticker_kind = kind
            self._ticker_buf += "\n"
        self._ticker_buf = (self._ticker_buf + text)[-2000:]

    def ticker_line(self):
        if self.active_turn_queue is None:
            return None
        for line in reversed(self._ticker_buf.splitlines()):
            line = line.strip()
            if line:
                return line
        # Turn in flight but nothing streamed yet: at high effort the model
        # reasons before it writes, and summaries arrive in bursts — show the
        # wait itself so the silence doesn't read as a hang.
        return f"thinking · {int(time.monotonic() - self._turn_t0)}s"

    def new_session(self, cwd=None, model=None, effort=None):
        # Under the turn lock: replacing the session DISCONNECTS the old one,
        # which would kill a concurrent in-flight turn mid-stream (seen live
        # 2026-08-13: two parallel new-chat requests, the second's new_session
        # orphaned the first's turn). Swap only between turns.
        with self.turn_lock:
            return self._new_session_locked(cwd, model, effort)

    def _new_session_locked(self, cwd, model, effort):
        old, self._session = self._session, None
        if old is not None:
            try:
                self._call(old.disconnect(), timeout=15)
            except BackendError as e:
                _log(f"WARN: old session disconnect failed: {e}")
        kwargs = {
            "working_directory": cwd or self._cwd,
            "streaming": True,
            "available_tools": [],
            "on_permission_request": self._deny_permission,
            # Ask for streamed thinking summaries where the model supports
            # them — they feed the console ticker during long reasoning
            # stretches (and act as inactivity-timeout keepalives).
            "reasoning_summary": "concise",
        }
        pin = model or self.default_model
        eff = effort or self.default_effort
        # "auto" is a models.list entry now (2026-08), and get_current reports
        # it as the live model — so clients read it off /v1/models and echo it
        # back as a pin. The runtime hard-rejects that pin together with an
        # effort ("Reasoning effort is not supported when using the `auto`
        # model"), while an UNPINNED session — which is auto — accepts one.
        # Same session either way, so drop the pin, never the effort.
        if pin and pin.lower() == "auto":
            pin = None
        if pin:
            kwargs["model"] = pin
        if eff:
            kwargs["reasoning_effort"] = eff
        try:
            session = self._call(self._client.create_session(**kwargs), timeout=120)
        except BackendError as e:
            # A disallowed pin usually gets silently overridden (checked
            # below), but if the runtime ever hard-rejects it, fall back to
            # an unpinned session (resolves to "auto") rather than dying.
            if "model" not in kwargs:
                raise
            _log(f"WARN: create_session with model={kwargs['model']!r} failed ({e}); "
                 f"retrying unpinned (auto)")
            del kwargs["model"]
            session = self._call(self._client.create_session(**kwargs), timeout=120)
        session.on(self._on_event)
        self._session = session
        self.session_id = session.session_id
        self.session_fresh = True
        self._current_effort = eff
        _log(f"SDK session: {self.session_id}"
             + (f" (reasoning_effort={eff})" if eff else ""))
        # Org model policy (e.g. a Business plan restricted to "Auto") does not
        # fail a pinned create_session — it silently overrides the pin. Always
        # ask the runtime what it actually selected: current_model() must be
        # truthful (/health and /v1/models report it), so shout on mismatch.
        try:
            current = self._call(session.rpc.model.get_current(), timeout=15)
            self._current_model = current.model_id or pin
            if pin and current.model_id and current.model_id != pin:
                _log(f"WARN: requested model {pin!r} but session runs "
                     f"{current.model_id!r} — likely an org model-policy override")
        except BackendError as e:
            self._current_model = None
            _log(f"WARN: could not verify session model: {e}")
        return self.session_id

    def current_model(self):
        return self._current_model or self.default_model

    def _apply_selection(self, model_id, effort):
        """Converge the live session onto (model_id, effort) via
        session.model/switchTo. Called only inside the turn lock, so selection
        and turn are atomic — the runtime scopes model/effort per session,
        agentry's API scopes them per request; this is the impedance match.

        switchTo accepts arbitrary ids without error — get_current even
        parrots them back — while the turn silently runs on a fallback model
        (verified 2026-08-13 with a bogus id), so ids must be validated
        against models.list upstream (the HTTP layer does). get_current still
        catches org-policy overrides of valid ids."""
        kw = {"reasoning_effort": effort} if effort else {}
        self._call(self._session.set_model(model_id, **kw), timeout=30)
        actual = model_id
        try:
            current = self._call(self._session.rpc.model.get_current(), timeout=15)
            actual = current.model_id or model_id
        except BackendError as e:
            _log(f"WARN: could not verify model switch: {e}")
        if actual != model_id:
            _log(f"WARN: requested model {model_id!r} but session runs {actual!r}")
        self._current_model = actual
        if effort:
            self._current_effort = effort
        _log(f"SDK model -> {actual}" + (f" @ {effort}" if effort else ""))

    def list_models(self):
        """Models available to this account from the runtime's models.list,
        as raw dicts (id, name, billing.tokenPrices in credits/1M tokens,
        modelPickerPriceCategory, ...). Fetched once and cached."""
        if self._models_cache is None:
            ml = self._call(self._client.rpc.models.list(self._ModelsListRequest()),
                            timeout=30)
            self._models_cache = [m.to_dict() for m in ml.models]
        return self._models_cache

    # The Copilot runtime's own usage ledger, shared by every SDK/CLI session
    # on this machine (verified 2026-08-13: agentry's SDK turns land in it,
    # no interactive copilot-cli needed).
    _USAGE_DB = Path.home() / ".copilot" / "session-store.db"

    # How often to re-fetch the account-wide baseline. The heartbeat asks for
    # status once a second, so this must never be an RPC-per-call — and the
    # re-fetch spawns a throwaway runtime (see _refresh_plan_quota), so it is
    # a process launch, not just an RPC. Between fetches the local ledger
    # keeps the figure live.
    _PLAN_QUOTA_TTL = 900.0
    # How often the heartbeat may re-sum the local ledger (full scan of a
    # few-thousand-row table: cheap, but not once a second).
    _LEDGER_TTL = 10.0

    def _refresh_plan_quota(self, client=None):
        """Fetch the account-wide credit allowance via account/getQuota and
        cache it as a baseline in self._plan_quota. Runs on a background thread
        (see _plan_quota_line); best-effort — on failure the last known value
        stands rather than the line going blank.

        THE RUNTIME CACHES THIS ANSWER FOR ITS WHOLE PROCESS LIFETIME (found
        2026-09-11: the console's runtime, up since 09-08, still answered
        190/5,000 while a fresh process answered 5,000/5,000; two calls 40 s
        apart in one process returned byte-identical reset_date stamps). So
        asking our own long-lived self._client again is pointless after the
        first time. `client=None` therefore spawns a THROWAWAY CopilotClient
        (its own runtime process, ~2-3 s), reads, and stops it; the startup
        prime passes self._client because that runtime is brand new anyway.

        Field semantics, established empirically 2026-09-01 against a live
        account — the names are misleading:
          used_requests        AI CREDITS used this calendar month, account-wide
                               (not a request count: it read 65 against a local
                               ledger sum of 65.44 credits for the same period,
                               and against github.com/billing's own '65 / 5,000
                               AI credits').
          entitlement_requests the month's credit allowance (5000 here).
        The period is the CALENDAR MONTH, resetting on the 1st. The billing
        page's 'resets in 30 days on Sep 30, 2026' is not a rolling window —
        September simply has 30 days, and Sep 30 is the period's last covered
        day. Confirmed two ways: the reset landed on Sep 1, and a rolling
        window ending Sep 30 would have started Aug 31 and included that
        evening's 58.89 credits, putting used near 124 instead of the 65
        observed.
          reset_date           NOT a reset instant: it is the runtime's FETCH
                               timestamp (ISO UTC, same format as the ledger's
                               created_at). Useless as a horizon, but exactly
                               the baseline instant the ledger delta needs.
          overage              credits billed past the allowance; only non-zero
                               once used > entitlement.
          usage_allowed_with_exhausted_quota / overage_allowed_with_exhausted_quota
                               whether the allowance is a billing threshold
                               (overage) or a hard stop. This plan: hard stop —
                               turns fail until the 1st.
        The 'chat' and 'completions' snapshots are unlimited/zero here and
        carry no signal, so only premium_interactions is read."""
        try:
            if client is not None:
                res = self._call(
                    client.rpc.account.get_quota(self._AccountGetQuotaRequest()),
                    timeout=10)
            else:
                res = asyncio.run(self._fetch_quota_fresh())
            snap = res.quota_snapshots.get("premium_interactions")
            if snap is None:
                self._plan_quota = None
            else:
                self._plan_quota = {
                    "unlimited": bool(snap.is_unlimited_entitlement),
                    "used": snap.used_requests,
                    "cap": snap.entitlement_requests,
                    "overage": snap.overage or 0,
                    "overage_ok": bool(snap.overage_allowed_with_exhausted_quota
                                       or snap.usage_allowed_with_exhausted_quota),
                    "fetched_at": snap.reset_date or "",
                }
        except Exception as e:
            _log(f"plan quota refresh failed (keeping last known value): {e}")
        finally:
            self._plan_quota_at = time.monotonic()
            self._plan_quota_busy = False

    async def _fetch_quota_fresh(self):
        """account/getQuota from a throwaway runtime, so the answer is not the
        long-lived runtime's process-lifetime cache. Always stops the process."""
        client = self._CopilotClient(working_directory=self._cwd)
        await asyncio.wait_for(client.start(), 60)
        try:
            return await asyncio.wait_for(
                client.rpc.account.get_quota(self._AccountGetQuotaRequest()), 15)
        finally:
            try:
                await asyncio.wait_for(client.stop(), 15)
            except Exception:
                pass

    def _month_start_utc(self):
        return datetime.datetime.now(datetime.timezone.utc).date().replace(day=1).isoformat()

    def _ledger_credits_since(self, since):
        """Credits this machine's runtime ledger has recorded at or after the ISO
        UTC instant `since`. Cached for _LEDGER_TTL. None if the ledger is
        missing/locked/has drifted."""
        now = time.monotonic()
        c = self._ledger_cache
        if c and c[0] == since and now - c[1] < self._LEDGER_TTL:
            return c[2]
        try:
            db = sqlite3.connect(f"file:{self._USAGE_DB}?mode=ro", uri=True)
            try:
                nano = db.execute(
                    "select coalesce(sum(total_nano_aiu), 0) from "
                    "assistant_usage_events where created_at >= ?",
                    (since,)).fetchone()[0]
            finally:
                db.close()
            val = nano / 1e9
        except Exception:
            val = None
        self._ledger_cache = (since, now, val)
        return val

    def _plan_quota_line(self):
        """The account-wide line: baseline snapshot + everything this machine's
        ledger has billed since the snapshot was fetched. Kicks off a background
        re-fetch once the baseline is older than _PLAN_QUOTA_TTL. Never blocks:
        the caller (the heartbeat, once a second) always gets an answer from
        cached state.

        Why the sum: the snapshot alone goes stale (runtime cache, see
        _refresh_plan_quota) and the ledger alone misses other devices and
        surfaces billing to the same allowance. Baseline + local delta is live
        for this PC and only under-reports other devices' usage since the last
        fetch, which the next fetch corrects. A '~' marks the figure as
        estimated whenever the delta is non-zero."""
        if (not self._plan_quota_busy
                and time.monotonic() - self._plan_quota_at >= self._PLAN_QUOTA_TTL):
            self._plan_quota_busy = True
            threading.Thread(target=self._refresh_plan_quota, daemon=True,
                             name="copilot-quota-refresh").start()
        snap = self._plan_quota
        if not snap:
            return None
        if snap["unlimited"]:
            return "Copilot credits unlimited"
        month_start = self._month_start_utc()
        since, base = snap["fetched_at"], snap["used"]
        if not since or since < month_start:
            # Baseline predates this month's reset: only the ledger's
            # month-to-date counts until a fresh fetch lands.
            since, base = month_start, 0
        delta = self._ledger_credits_since(since)
        if delta is None:
            used, approx = base, ""
            note = f" (as of {since[11:16]}Z; ledger unavailable)"
        else:
            used, approx, note = base + delta, ("~" if delta else ""), ""
        cap = snap["cap"]
        head = f"Copilot credits {approx}{used:,.0f}/{cap:,} used this month"
        if used >= cap:
            over = used - cap
            if snap["overage_ok"]:
                return f"{head} · {over:,.0f} over, billed as overage{note}"
            return f"{head} · exhausted, turns blocked until the 1st{note}"
        return f"{head} · {approx}{cap - used:,.0f} left{note}"

    def quota_status(self):
        """One line for the console heartbeat, in AI credits (1 credit = $0.01).

        Leads with the account-wide figure — the same '65 / 5,000 AI credits'
        github.com/billing shows — because that is the number worth watching:
        a fetched baseline plus this machine's ledger since (_plan_quota_line).
        The ledger ALONE is only a FALLBACK for when no fetch has answered yet:
        it sums the same calendar month on this machine, so it under-reports
        the account (other devices and surfaces bill to the same allowance).

        The per-turn credits accumulated by this agentry process are appended as
        'this run', deliberately not 'session': copilot-cli's exit banner prints
        a SESSION total that can span a month boundary (its 123.23 was 58.89 on
        Aug 31 plus 64.34 on Sep 1), and the two must not look like the same
        quantity."""
        parts = []
        plan = self._plan_quota_line()
        if plan:
            parts.append(plan)
        else:
            parts.append(self._ledger_fallback()
                         or "Copilot credits: allowance unavailable")
        if self._session_credits:
            parts.append(f"this run {self._session_credits:.2f}")
        return " · ".join(parts) or None

    def _ledger_fallback(self):
        """Calendar-month credit total from this machine's runtime ledger, worded
        so it cannot be mistaken for the account allowance. Used only when
        account/getQuota has not answered."""
        credits = self._ledger_credits_since(self._month_start_utc())
        if credits is None:
            return None
        return (f"Copilot credits {credits:,.0f} used this month "
                f"on this PC (account allowance unavailable)")

    _IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                  "image/gif": ".gif", "application/pdf": ".pdf"}

    def prompt(self, text, images=None, timeout=900, model=None, effort=None):
        """Generator yielding text deltas for one turn. Requires an active session.

        model/effort: this turn's selection. The Copilot runtime scopes both
        per SESSION, so the session is switched under the turn lock when they
        differ from its current state — selection and turn are atomic.

        Images ride as SDK *file* attachments via temp files: that is the
        runtime's native vision path. Blob attachments are accepted by the
        API but land as opaque file context — the model never actually sees
        the pixels (verified: a red/blue test image drew hallucinated colors
        as a blob, correct ones as a file). Undecodable images are dropped
        loudly, per the Backend contract."""
        if not self._session:
            raise BackendError("no active session (call new_session first)")
        attachments, tmp_paths = [], []
        for i, (mime, data) in enumerate(images or []):
            try:
                raw = base64.b64decode(data, validate=True)
            except Exception as e:
                _log(f"WARN: dropping undecodable image {i} ({mime}): {e}")
                continue
            ext = self._IMAGE_EXT.get(mime, ".bin")
            with tempfile.NamedTemporaryFile(prefix="agentry_img_", suffix=ext,
                                             delete=False) as f:
                f.write(raw)
                tmp_paths.append(f.name)
            attachments.append({"type": "file", "path": tmp_paths[-1],
                                "displayName": f"image-{i}{ext}"})
        try:
            with self.turn_lock:
                want_model = model or self.default_model
                want_effort = effort or self.default_effort
                if ((want_model and want_model != self._current_model)
                        or (want_effort and want_effort != self._current_effort)):
                    target = want_model or self._current_model
                    if target:
                        try:
                            self._apply_selection(target, want_effort)
                        except BackendError as e:
                            yield f"\n[copilot error] model/effort switch failed: {e}"
                            return
                    else:
                        _log("WARN: effort requested but active model unknown; "
                             "keeping session defaults")
                q = queue.Queue()
                self._ticker_buf = ""
                self._ticker_kind = None
                self._turn_t0 = time.monotonic()
                self._turn_credits = 0.0
                self.active_turn_queue = q
                try:
                    self._call(self._session.send(text, attachments=attachments or None),
                               timeout=30)
                    self.session_fresh = False
                    while True:
                        try:
                            kind, payload = q.get(timeout=timeout)
                        except queue.Empty:
                            self.cancel()   # stop the server-side turn we're abandoning
                            yield f"\n[copilot timeout after {timeout}s]"
                            return
                        if kind == "delta":
                            yield payload
                        elif kind == "reasoning":
                            yield ("reasoning", payload)
                        elif kind == "keepalive":
                            continue    # activity tick; resets the q.get() window
                        elif kind == "error":
                            yield f"\n[copilot error] {payload}"
                            return
                        elif kind == "idle":
                            if self._turn_credits:
                                _log(f"turn cost {self._turn_credits:.3f} credits"
                                     f"  (session total {self._session_credits:.3f})")
                            return
                finally:
                    self.active_turn_queue = None
        finally:
            for p in tmp_paths:       # turn is over; the runtime has read them
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def cancel(self):
        if not self._session:
            return False
        try:
            # Fire-and-forget: abort the in-flight turn, don't block the
            # HTTP handler on the round-trip.
            asyncio.run_coroutine_threadsafe(self._session.abort(), self._loop)
            return True
        except Exception:
            return False

    def is_alive(self):
        return self._alive and self._loop.is_running()

    def close(self):
        self._alive = False
        if self._session is not None:
            try:
                self._call(self._session.disconnect(), timeout=10)
            except Exception:
                pass
            self._session = None
        try:
            self._call(self._client.stop(), timeout=15)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        if self._log_handler:
            try:
                logging.getLogger("copilot").removeHandler(self._log_handler)
                self._log_handler.close()
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

    Unlike the Copilot backend, model and reasoning effort are TURN-level
    params (turn/start), not session config — prompt() simply stamps each
    turn with its request's selection. Auth is the ChatGPT account login
    (`codex login`); no OPENAI_API_KEY needed.

    Reference: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
    """

    # Reasoning levels codex accepts on turn/start. model/list (0.147.0)
    # advertises low..max plus "ultra" ("maximum reasoning with automatic
    # task delegation") on the gpt-5.6 models; none/minimal predate that.
    EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}

    # codex is an AGENT: on a non-trivial prompt it will try to use its shell
    # tool to explore the cwd for context (e.g. grepping for JSON field names
    # it sees in an enrichment prompt). For agentry's pure-chat use that is
    # wrong, wasteful, and a privacy risk. This developer instruction tells it
    # to behave as a stateless answerer. (Necessary but not sufficient — see
    # the empty-scratch cwd below; sandbox=read-only alone does NOT stop reads,
    # because read-only commands are auto-approved regardless of approvalPolicy.)
    # Image generation is the ONE carve-out (codex-cli >= 0.149 ships it as a
    # built-in tool, stable, on by default). Probed 2026-09-12 on 0.154.0
    # (`_bench/codex_imagegen_probe.py`): with the flat "Do not use any tools"
    # wording codex refuses ("I'm unable to generate images in this chat");
    # with this carve-out it generates, under the same approvalPolicy=never /
    # sandbox=read-only / empty-cwd setup, with no approval round-trip. The
    # "only when explicitly asked" clause keeps enrichment prompts image-free.
    CHAT_ONLY_INSTRUCTIONS = (
        "You are a stateless question-answering assistant exposed over an HTTP "
        "chat API. Answer each user message directly and completely using only "
        "your own knowledge and the content of the message itself. "
        "The ONLY tool you may use is image generation, and only when the user "
        "explicitly asks for an image; never use any other tool. "
        "Do not run shell commands. Do not read, list, "
        "search, or otherwise inspect files or directories. There is no relevant "
        "codebase, repository, or workspace — ignore the working directory "
        "entirely. If the message asks for a specific output format (e.g. a JSON "
        "object), return exactly that and nothing else."
    )

    def __init__(self, codex_path="codex", cwd=None, model=None,
                 reasoning_effort="low", developer_instructions=None, log_path=None):
        # Launcher defaults — immutable after construction; per-request
        # selection rides through prompt(model=, effort=). default_model=None
        # omits the turn-level override, so each thread runs on codex's own
        # configured default (~/.codex/config.toml, i.e. whatever was last
        # selected in the codex TUI). This tracks OpenAI's model migrations
        # (e.g. gpt-5.4-mini -> gpt-5.6-luna) without a code change; the
        # thread/start log line below shows what each thread resolved to.
        self.default_model = model
        self.default_effort = reasoning_effort
        self._default_model = None   # resolved by thread/start; see new_session
        self._turn_model = None      # the in-flight turn's model, for the rate card
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
        # Resolve through PATH: npm installs ship codex only as .cmd/.ps1
        # shims (no .exe since ~0.14x), which a bare Popen("codex") can't
        # find (WinError 2). which() honors PATHEXT; CreateProcess runs a
        # full-path .cmd via cmd.exe on its own.
        cmd = [shutil.which(codex_path) or codex_path, "app-server"]
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
        self._active_turn_id = None    # turn id from the turn/start ack, for turn/interrupt
        self._active_thread_id = None  # thread the in-flight turn runs on (may be a scratch thread)
        self._rate_limits = None       # latest RateLimitSnapshot from notifications
        self._rl_lock = threading.Lock()
        self._turn_tokens = None       # thread/tokenUsage/updated "last" for the in-flight turn
        self._session_credits_est = 0.0  # running estimate from the rate card
        self._models_cache = None      # model/list result, fetched on demand
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

    def new_session(self, cwd=None, model=None, effort=None):
        # model/effort are turn-level overrides (TurnStartParams), so thread/start
        # only carries session-scoped policy — the model/effort params exist
        # for Backend-interface parity and are deliberately unused here.
        # Serialized with turns so a concurrent new-chat can't swap session_id
        # out from under an in-flight turn (same guard as the copilot backend).
        # We pin an empty cwd and inject
        # chat-only developer instructions so codex behaves as a plain answerer
        # rather than an agent exploring the filesystem.
        with self.turn_lock:
            self.session_id, self._default_model = self._start_thread(cwd)
            self.session_fresh = True
            _log(f"codex thread: {self.session_id} (default model={self._default_model!r})")
            return self.session_id

    def _start_thread(self, cwd=None):
        """thread/start with agentry's locked-down policy; returns (thread id,
        the model the thread resolved to). Does NOT touch session_id."""
        params = {"approvalPolicy": "never", "sandbox": "read-only",
                  "cwd": os.path.abspath(cwd) if cwd else self.cwd,
                  # Without this config override codex emits NO reasoning
                  # notifications at all (verified 2026-08-13 on 0.147.0:
                  # high-effort turns stayed silent until it was set; no
                  # experimental capability needed).
                  "config": {"model_reasoning_summary": "detailed"}}
        if self.developer_instructions:
            params["developerInstructions"] = self.developer_instructions
        result = self._request("thread/start", params)
        # Remember what the thread resolved to so current_model() is truthful
        # when no explicit pin is set (the label used to lie: "codex-default").
        return result["thread"]["id"], result.get("model")

    def scratch_thread(self):
        """A throwaway thread for one-shot work (the Images routes), separate
        from the chat session: a shared thread would accumulate every
        reference and result image in its context (probe: input tokens grew
        15.7k -> 24k over three image calls) and the tool's
        `num_last_images_to_include` could then pick a stale image as the
        reference. The id is passed back via prompt(thread_id=) and simply
        dropped afterwards; codex keeps its rollout under ~/.codex/sessions."""
        tid, _ = self._start_thread()
        _log(f"codex scratch thread: {tid}")
        return tid

    def current_model(self):
        return self.default_model or self._default_model

    def list_models(self):
        """Models from the app-server's model/list (id, displayName,
        supportedReasoningEfforts, ...). Fetched once and cached."""
        if self._models_cache is None:
            result = self._request("model/list", {}, timeout=15)
            self._models_cache = result.get("data") or []
        return self._models_cache or None

    def prompt(self, text, images=None, timeout=900, model=None, effort=None,
               thread_id=None):
        """Generator yielding text deltas for one turn. Requires an active thread.

        model/effort: this turn's selection — codex scopes both per turn
        natively (TurnStartParams), so they map straight onto turn/start.

        Images ride as ImageUserInput items ({type:"image", url}) with a data:
        URI — the UserInput schema (generate-json-schema) also offers
        localImage{path} as a fallback if data: URLs turn out unsupported.

        thread_id (codex-only extension): run the turn on that thread instead
        of the chat session — see scratch_thread(). Serialized by the same
        turn lock either way."""
        thread_id = thread_id or self.session_id
        if not thread_id:
            raise BackendError("no active session (call new_session first)")
        input_items = []
        if text:
            input_items.append({"type": "text", "text": text})
        for mime, data in (images or []):
            input_items.append({"type": "image", "url": f"data:{mime};base64,{data}"})
        with self.turn_lock:
            q = queue.Queue()
            self.active_turn_queue = q
            try:
                # turn/start's response only acknowledges; the answer streams as
                # notifications terminating with turn/completed.
                msg_id = self._next_id()
                ack = queue.Queue(maxsize=1)
                self.pending[msg_id] = ack
                self._turn_tokens = None
                turn_model = model or self.default_model
                turn_effort = effort or self.default_effort
                self._turn_model = turn_model or self._default_model
                self._active_thread_id = thread_id   # for turn/interrupt
                tparams = {"threadId": thread_id,
                           "input": input_items}
                if turn_model:
                    tparams["model"] = turn_model
                if turn_effort:
                    tparams["effort"] = turn_effort
                self._write({"jsonrpc": "2.0", "id": msg_id,
                             "method": "turn/start", "params": tparams})
                if thread_id == self.session_id:
                    self.session_fresh = False
                # The ack normally arrives at once (notifications buffer in q
                # meanwhile). An error response (bad model, dead thread) means
                # no turn ever starts — surface it now instead of stalling
                # until the notification timeout. On success it carries the
                # turn id, needed for turn/interrupt and stale-turn filtering.
                try:
                    resp = ack.get(timeout=30)
                except queue.Empty:
                    yield "\n[codex error] no response to turn/start after 30s"
                    return
                if "error" in resp:
                    err = resp["error"]
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    yield f"\n[codex error] {msg}"
                    return
                turn_id = ((resp.get("result") or {}).get("turn") or {}).get("id")
                self._active_turn_id = turn_id
                while True:
                    try:
                        method, params = q.get(timeout=timeout)
                    except queue.Empty:
                        self._interrupt(turn_id)   # stop the turn we're abandoning
                        yield f"\n[codex timeout after {timeout}s]"
                        return
                    if not isinstance(params, dict):
                        continue
                    if method == "item/agentMessage/delta":
                        # A turn abandoned by timeout can keep streaming; drop
                        # anything tagged with a different turn id.
                        if turn_id and params.get("turnId") not in (None, turn_id):
                            continue
                        t = params.get("delta")
                        if t:
                            yield t
                    elif method in ("item/reasoning/summaryTextDelta",
                                    "item/reasoning/textDelta"):
                        # Streamed thinking (requires the model_reasoning_summary
                        # config on thread/start). Same tagged-tuple contract as
                        # the copilot backend; the web UI renders it as the
                        # collapsible thinking block.
                        if turn_id and params.get("turnId") not in (None, turn_id):
                            continue
                        t = params.get("delta")
                        if t:
                            yield ("reasoning", t)
                    elif method in ("item/started", "item/completed"):
                        # Built-in image generation (gpt-image-2, pinned by
                        # codex at quality=auto/size=auto; not client-tunable).
                        # The finished item carries the whole PNG as base64 in
                        # `result` (~1 MB for a 1254x1254) plus a copy on disk
                        # under ~/.codex/generated_images/<thread>/<item>.png.
                        item = params.get("item") or {}
                        if item.get("type") != "imageGeneration":
                            continue
                        if turn_id and params.get("turnId") not in (None, turn_id):
                            continue
                        if method == "item/started":
                            self._image_t0 = time.time()
                            _log("codex image generation started")
                            continue
                        yield self._image_from_item(item)
                    elif method == "thread/tokenUsage/updated":
                        # Per-turn token accounting; "last" is this turn's call.
                        self._turn_tokens = (params.get("tokenUsage")
                                             or {}).get("last") or {}
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if turn_id and turn.get("id") not in (None, turn_id):
                            continue
                        status = turn.get("status")
                        if status == "failed":
                            err = (turn.get("error") or {}).get("message", "unknown")
                            yield f"\n[codex error] {err}"
                        _log(f"turn status={status}" + self._usage_suffix())
                        return
            finally:
                self.active_turn_queue = None
                self._active_turn_id = None
                self._active_thread_id = None
                self.pending.pop(msg_id, None)

    # PNG / JPEG / WebP base64 prefixes; codex's image tool emits PNG today.
    _IMAGE_MAGIC = (("iVBORw0KGgo", "image/png"), ("/9j/", "image/jpeg"),
                    ("UklGR", "image/webp"))

    def _image_from_item(self, item):
        """Turn a completed imageGeneration item into the Backend contract's
        ("image", mime, b64, revised_prompt) tuple, or a visible error note
        when codex reports a failure (usage-limit) or an empty result."""
        dt = time.time() - (getattr(self, "_image_t0", None) or time.time())
        failure = item.get("failure") or {}
        b64 = item.get("result") or ""
        if failure.get("type") == "usageLimitExceeded" or not b64:
            reason = ("image usage limit reached"
                      if failure.get("type") == "usageLimitExceeded"
                      else f"status={item.get('status')!r}, empty result")
            resets = failure.get("resetsAt")
            if resets:
                reason += f" (resets {self._fmt_reset(resets)})"
            _log(f"codex image failed after {dt:.1f}s: {reason}")
            return f"\n[codex image error] {reason}"
        mime = next((m for magic, m in self._IMAGE_MAGIC if b64.startswith(magic)),
                    "image/png")
        # Size + wall time only — the prompt itself stays out of the log.
        _log(f"codex image: {len(b64) * 3 // 4 / 1024:.0f} KB {mime} in {dt:.1f}s"
             + (f" (saved {item['savedPath']})" if item.get("savedPath") else ""))
        return ("image", mime, b64, item.get("revisedPrompt"))

    def _interrupt(self, turn_id):
        """Fire-and-forget turn/interrupt. Per the v2 schema it is a REQUEST
        requiring both threadId and turnId (a bare-threadId notification is
        silently ignored); we send a real id and drop the response unread."""
        thread_id = getattr(self, "_active_thread_id", None) or self.session_id
        if not (thread_id and turn_id):
            return False
        try:
            self._write({"jsonrpc": "2.0", "id": self._next_id(),
                         "method": "turn/interrupt",
                         "params": {"threadId": thread_id,
                                    "turnId": turn_id}})
            return True
        except Exception:
            return False

    def cancel(self):
        return self._interrupt(self._active_turn_id)

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
        # Pay-as-you-go credit balance (Plus and up can buy credits that
        # extend past the included windows) and this process's rate-card
        # estimate of what it has burned.
        credits = rl.get("credits") or {}
        if credits.get("unlimited"):
            parts.append("credits unlimited")
        elif credits.get("hasCredits"):
            parts.append(f"credits {credits.get('balance')}")
        if self._session_credits_est:
            parts.append(f"session ~{self._session_credits_est:.2f} credits")
        if not parts:
            return None
        plan = rl.get("planType") or "codex"
        return f"codex {plan} quota | " + " | ".join(parts)

    # Codex credits per 1M tokens: (fresh input, cached input, output).
    # Source: the official rate card (help.openai.com article 20001106 /
    # learn.chatgpt.com/docs/pricing), as of 2026-08; 1 credit ≈ $0.04.
    # Estimates only — OpenAI can reprice without notice, and models not in
    # this table simply skip the estimate.
    _CREDITS_PER_MTOK = {
        "gpt-5.6-sol":   (125.0, 12.5, 750.0),
        "gpt-5.6-terra": (50.0,   5.0, 300.0),
        "gpt-5.6-luna":  (5.0,    0.5,  30.0),
    }

    def _estimate_credits(self, last):
        """Rate-card estimate for one turn's tokenUsage 'last' block, in Codex
        credits; None when the model is unknown or no usage arrived."""
        rates = self._CREDITS_PER_MTOK.get(
            ((self._turn_model or self.current_model()) or "").lower())
        if not rates or not last:
            return None
        cached = last.get("cachedInputTokens") or 0
        fresh = max(0, (last.get("inputTokens") or 0) - cached)
        out = last.get("outputTokens") or 0
        return (fresh * rates[0] + cached * rates[1] + out * rates[2]) / 1e6

    def _usage_suffix(self):
        """'  tokens in=12576 (cached 12032) out=5  ~0.014 credits (session ~0.03)
        quota: weekly 99% left' — whichever parts are known."""
        parts = []
        tu = self._turn_tokens
        if tu:
            parts.append(f"tokens in={tu.get('inputTokens', 0)} "
                         f"(cached {tu.get('cachedInputTokens', 0)}) "
                         f"out={tu.get('outputTokens', 0)}")
            est = self._estimate_credits(tu)
            if est is not None:
                self._session_credits_est += est
                parts.append(f"~{est:.3f} credits "
                             f"(session ~{self._session_credits_est:.3f})")
        brief = self.quota_brief()
        if brief:
            parts.append(f"quota: {brief}")
        return ("  " + "  ".join(parts)) if parts else ""

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


# --- Claude Code backend (persistent stream-json) -----------------------

class ClaudeCodeBackend(Backend):
    """Reuse a lean Claude process, with a confirmed /clear before each task.

    Conversation isolation is per prompt, not per HTTP new-chat heuristic.
    Both conversation_reset and its successful result must arrive before the
    next prompt is sent. A failed reset disables reuse for this backend and
    falls back to fresh processes. Model changes also start a fresh worker.
    Cancellation, disconnect, EOF and failed turns retire the worker; output
    from an abandoned process can never feed its replacement's queue.

    Requires Claude Code with --safe-mode (validated on 2.1.281). Unlike --bare,
    safe mode retains normal subscription authentication. No MCP/tools/custom
    hooks/plugins/memory discovery or saved sessions are needed by this relay.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"
    RESET_TIMEOUT = 5.0
    LEAN_FLAGS = ["--safe-mode", "--strict-mcp-config", "--tools", "",
                  "--no-session-persistence"]

    def __init__(self, claude_path="claude", cwd=None, model=None,
                 reasoning_effort=None, log_path=None):
        self.claude_path = shutil.which(claude_path) or claude_path
        self.default_model = model or self.DEFAULT_MODEL
        # Preserved behavior: agentry does not yet forward Claude effort.
        # Recent CLIs do have --effort; wiring it is separate from process reuse.
        self.default_effort = reasoning_effort
        self.cwd = os.path.abspath(cwd) if cwd else os.path.join(
            tempfile.gettempdir(), "agentry-claude-scratch")
        os.makedirs(self.cwd, exist_ok=True)
        self.session_id = None
        self.session_fresh = False
        self.turn_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._proc = None
        self._events = None
        self._worker_model = None
        self._dirty = False
        self._reuse = True
        self._closed = False
        self._cancel_event = None
        self._rate_limit = None
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

    def _check_cancelled(self):
        if self._closed:
            raise BackendError("Claude backend is closed")
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise BackendError("Claude turn cancelled")

    def new_session(self, cwd=None, model=None, effort=None):
        # Logical HTTP session only. Reset is always inside prompt's lock,
        # even if multiple requests race through the HTTP freshness check.
        with self.turn_lock:
            self._check_cancelled()
            if cwd and os.path.abspath(cwd) != self.cwd:
                self._stop_worker()
                self.cwd = os.path.abspath(cwd)
                os.makedirs(self.cwd, exist_ok=True)
            self._ensure_worker(model or self.default_model)
            self.session_id = uuid.uuid4().hex
            self.session_fresh = True
            return self.session_id

    def _read_stdout(self, proc, events):
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    _log(f"claude non-JSON line: {line[:120]!r}")
                    continue
                if isinstance(msg, dict):
                    events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            events.put(None)

    def _drain_stderr(self, proc):
        try:
            for line in proc.stderr:
                if line.strip():
                    _log(f"claude stderr: {line.rstrip()[:200]}")
        except (OSError, ValueError):
            pass

    def _ensure_worker(self, model):
        self._check_cancelled()
        proc = self._proc
        if (proc is not None and proc.poll() is None
                and self._worker_model == model):
            return
        self._stop_worker()
        cmd = [self.claude_path, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--include-partial-messages",
               "--verbose", "--model", model, *self.LEAN_FLAGS]
        events = queue.Queue()
        # Publish atomically with cancellation. Do not hold this lock while
        # waiting on model output: /v1/cancel must remain responsive.
        with self._proc_lock:
            self._check_cancelled()
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                encoding="utf-8", errors="replace", cwd=self.cwd)
            self._proc, self._events = proc, events
            self._worker_model, self._dirty = model, False
        threading.Thread(target=self._read_stdout, args=(proc, events),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        _log(f"claude worker started (model={model}, pid={proc.pid})")

    def _stop_worker(self):
        with self._proc_lock:
            proc, self._proc = self._proc, None
            self._events = None
            self._worker_model, self._dirty = None, False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass  # it may have exited between poll and terminate
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass

    def _send(self, text):
        self._check_cancelled()
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        proc = self._proc
        if proc is None:
            raise BackendError("Claude worker stopped")
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except ValueError as e:
            raise BackendError("Claude input pipe closed") from e

    def _next_event(self, timeout):
        self._check_cancelled()
        events = self._events
        if events is None:
            raise BackendError("Claude worker stopped")
        try:
            msg = events.get(timeout=max(0, timeout))
        except queue.Empty:
            raise BackendError("Claude response timed out")
        self._check_cancelled()
        if msg is None:
            raise BackendError("Claude exited before completing the turn")
        self._log_wire("<<", msg)
        if msg.get("type") == "rate_limit_event":
            with self._rl_lock:
                self._rate_limit = msg.get("rate_limit_info")
        return msg

    @staticmethod
    def _check_result(msg):
        if msg.get("is_error") or msg.get("subtype") != "success":
            raise BackendError(str(msg.get("result") or msg.get("subtype")
                                   or "Claude returned an unsuccessful result"))

    def _reset_worker(self):
        self._send("/clear")
        deadline = time.monotonic() + self.RESET_TIMEOUT
        reset_seen = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("Claude reset timed out")
            msg = self._next_event(remaining)
            if msg.get("type") == "conversation_reset":
                reset_seen = True
            elif msg.get("type") == "result":
                self._check_result(msg)
                if not reset_seen:
                    raise BackendError("Claude reset was not acknowledged")
                # Consume the reset result too; otherwise it could be mistaken
                # for the next task's successful completion.
                self._dirty = False
                return

    def prompt(self, text, images=None, timeout=900, model=None, effort=None):
        """Stream one independent task. Reuse only a confirmed clean worker."""
        if not self.session_id:
            raise BackendError("no active session (call new_session first)")
        if images:
            yield (f"[agentry: {len(images)} image(s) dropped — "
                   "claude backend is text-only for now]\n")
        with self.turn_lock:
            with self._proc_lock:
                self._cancel_event = threading.Event()
            complete = False
            try:
                target = model or self.default_model
                self._ensure_worker(target)
                if self._dirty:
                    if self._reuse:
                        try:
                            self._reset_worker()
                        except (BackendError, OSError) as e:
                            self._check_cancelled()
                            _log(f"WARN: {e}; using fresh Claude workers for isolation")
                            self._reuse = False
                            self._stop_worker()
                            self._ensure_worker(target)
                    else:
                        self._stop_worker()
                        self._ensure_worker(target)
                self._check_cancelled()
                self._dirty = True
                self.session_fresh = False
                self._send(text)
                got_text = False
                while True:
                    msg = self._next_event(timeout)
                    kind = msg.get("type")
                    if kind == "stream_event":
                        event = msg.get("event") or {}
                        if event.get("type") == "content_block_delta":
                            delta = (event.get("delta") or {}).get("text")
                            if delta:
                                got_text = True
                                yield delta
                    elif kind == "assistant" and not got_text:
                        for block in (msg.get("message") or {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                got_text = True
                                yield block["text"]
                    elif kind == "conversation_reset":
                        raise BackendError("unexpected conversation reset during task")
                    elif kind == "result":
                        self._check_result(msg)
                        complete = True
                        _log(f"claude turn complete dur={msg.get('duration_ms')}ms")
                        return
            except (BackendError, OSError) as e:
                # Retire before yielding the error: even callers that pause
                # iteration must not leave a failed model turn running.
                self._stop_worker()
                yield f"\n[claude error] {e}"
            finally:
                if not complete:
                    self._stop_worker()
                with self._proc_lock:
                    self._cancel_event = None

    def cancel(self):
        with self._proc_lock:
            if self._cancel_event is None:
                return False
            self._cancel_event.set()
            # Wake the waiter even if the process hasn't been published yet,
            # or its stdout reader is delayed.
            if self._events is not None:
                self._events.put(None)
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            return True

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
        # This reusable wrapper respawns a dead worker on the next task.
        return not self._closed

    def close(self):
        with self._proc_lock:
            self._closed = True
        self.cancel()
        # Do not acquire turn_lock: a generator can be suspended at yield
        # while shutdown is requested from its own thread.
        self._stop_worker()
        if self._logf:
            self._logf.close()
            self._logf = None


# --- Factory -------------------------------------------------------------

# --- Grok Build backend (ACP over stdio) ---------------------------------

class GrokACPBackend(Backend):
    """xAI Grok Build (`grok agent stdio`) over the Agent Client Protocol.

    One persistent process; one ACP session per chat (session/new is ~0.6 s,
    so the HTTP new-chat heuristic maps straight onto it). Model and effort
    are session state in ACP (`session/set_model`,
    `session/set_config_option reasoning_effort`), so prompt() re-applies the
    request's selection inside the turn lock before `session/prompt`.

    Read-only enforcement is the agent profile (`grok-agent-profile.md`):
    `grok agent` accepts none of the headless tool/permission flags, an empty
    `tools:` list is ignored, and permission prompting never reaches the
    client (validated 2026-10-04 on 1.0.46, archive/GROK-PLAN.md). The
    profile's non-empty allowlist leaves only `image_gen` plus inert MCP
    discovery stubs; agent->client requests are rejected anyway.

    The ACP prompt takes no image content (`promptCapabilities.image: false`),
    so attachments go to disk under <cwd>/refs/<session>/ and the message
    names the paths; the model sees them through `read_file` (its result is
    an image block) and can hand them to `image_edit`. Every tool call is
    gated by a client hook registered on session/new (`_meta["x.ai/hooks"]`,
    reverse request `_x.ai/hooks/run`): paths outside the refs dir and grok's
    own image output dir are denied, as is any tool the profile should not
    have left in. Image output rides `image_gen`/`image_edit`: grok writes a
    JPEG under ~/.grok/sessions/<cwd>/<session>/images/ and reports the path;
    we read it back into the ("image", ...) tuple. Sessions and images
    persist there by design; the user cleans up.
    """

    DEFAULT_MODEL = "grok-4.7"
    DEFAULT_EFFORT = "high"
    # Listed by the CLI but not exposed: "Fast variant. 2x the price" buys
    # nothing on a flat subscription except double quota burn.
    HIDDEN_MODELS = {"grok-4.7-build-fast"}
    # agentry's effort vocabulary -> grok's (xhigh/high/medium/low).
    EFFORT_MAP = {"none": "low", "minimal": "low", "max": "xhigh", "ultra": "xhigh"}
    EFFORT_LADDER = ["low", "medium", "high", "xhigh"]
    PROFILE_PATH = Path(__file__).parent / "grok-agent-profile.md"
    # Housekeeping pushed by grok that no turn needs to see.
    _NOISE = {"_x.ai/models/update", "_x.ai/announcements/update",
              "_x.ai/settings/update", "_x.ai/session/setup",
              "_x.ai/queue/changed", "_x.ai/sessions/changed",
              "_x.ai/mcp/servers_updated", "_x.ai/mcp_initialized"}
    _IMAGE_MAGIC = ((b"\xff\xd8", "image/jpeg"), (b"\x89PNG", "image/png"),
                    (b"RIFF", "image/webp"))

    def __init__(self, grok_path="grok", cwd=None, model=None,
                 reasoning_effort=None, log_path=None, profile_path=None):
        self.default_model = model or self.DEFAULT_MODEL
        self.default_effort = reasoning_effort or self.DEFAULT_EFFORT
        self.profile_path = Path(profile_path) if profile_path else self.PROFILE_PATH
        if not self.profile_path.is_file():
            raise BackendError(f"grok agent profile missing: {self.profile_path}")
        # Stable empty scratch cwd: grok persists every session under
        # ~/.grok/sessions/<urlencoded cwd>/, so a fixed cwd keeps all of
        # agentry's sessions in one folder.
        self.cwd = os.path.abspath(cwd) if cwd else os.path.join(
            tempfile.gettempdir(), "agentry-grok-scratch")
        os.makedirs(self.cwd, exist_ok=True)
        # Where attachments land, and the only places the hook lets tools
        # read: our refs dir, and grok's own session store (so "edit the
        # image you just made" can name its previous output).
        self.refs_root = os.path.join(self.cwd, "refs")
        # A fresh process has no live sessions: whatever a crashed or killed
        # predecessor left here is garbage (copies of client uploads).
        shutil.rmtree(self.refs_root, ignore_errors=True)
        self._allowed_roots = [self._canon(self.refs_root),
                               self._canon(os.path.join(Path.home(), ".grok", "sessions"))]
        # XAI_API_KEY takes precedence over the grok.com login and bills the
        # metered API instead of the subscription. Never let it through.
        env = {k: v for k, v in os.environ.items() if k != "XAI_API_KEY"}
        if "XAI_API_KEY" in os.environ:
            _log("grok: XAI_API_KEY is set in the environment; NOT passing it "
                 "to grok so turns bill the subscription, not the API")
        exe = shutil.which(grok_path)
        if not exe:
            for cand in (Path.home() / ".grok" / "bin" / "grok.exe",
                         Path.home() / ".grok" / "bin" / "grok"):
                if cand.is_file():
                    exe = str(cand)
                    break
        exe = exe or grok_path
        # Flag order matters: --agent-profile belongs to `grok agent`, not to
        # `stdio` (which takes nothing relevant).
        cmd = [exe, "agent", "--agent-profile", str(self.profile_path), "stdio"]
        _log(f"grok spawn: {' '.join(cmd)}  (cwd={self.cwd})")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            cwd=self.cwd, env=env,
        )
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending = {}               # id -> Queue for a response
        self.active_turn_queue = None   # (method, params) notifications of the in-flight turn
        self._turn_in_flight = False
        self._active_sid = None         # session the in-flight turn runs on
        self._video_sid = None          # session allowed to call the video tools
        self.session_id = None
        self.session_fresh = False
        self._sess_state = {}           # sessionId -> [model, effort] the session runs
        self._models = []               # availableModels from initialize
        self._cost_ticks = 0            # this-run sum of costUsdTicks (1e-10 USD)
        self._turn_usage = None
        self.turn_lock = threading.Lock()
        self.log_path = log_path
        self._logf = None
        if self.log_path:
            self.log_path.parent.mkdir(exist_ok=True)
            self._logf = open(self.log_path, "w", encoding="utf-8")

        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        self._initialize()

    # -- plumbing (same shape as the codex backend) --

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
            err = resp["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise BackendError(f"{method}: {msg}")
        return resp.get("result") or {}

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
                    _log(f"grok non-JSON line: {line[:120]!r}")
                    continue
                self._log_wire("<<", msg)
                if "id" in msg and ("result" in msg or "error" in msg):
                    q = self.pending.pop(msg["id"], None)
                    if q is not None:
                        q.put(msg)
                elif "id" in msg and "method" in msg:
                    if msg["method"] == self.HOOK_RUN_METHOD:
                        # Our pre_tool_use gate (registered on session/new).
                        # Answer here, on the reader thread: grok fails OPEN
                        # on a timeout or a malformed reply, so be quick and
                        # exact. (acp_session/hooks.rs: classify())
                        self._write({"jsonrpc": "2.0", "id": msg["id"],
                                     "result": self._hook_decision(msg.get("params") or {})})
                        continue
                    # Any other agent -> client request (permission, fs/*,
                    # terminal). Never observed with the profile; refuse.
                    _log(f"grok asked {msg['method']!r}; refused")
                    self._write({
                        "jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601,
                                  "message": f"method '{msg['method']}' not supported by client"},
                    })
                elif "method" in msg:
                    if msg["method"] in self._NOISE:
                        continue
                    q = self.active_turn_queue
                    if q is not None:
                        q.put((msg["method"], msg.get("params") or {}))
        except Exception as e:
            _log(f"grok reader exited: {e}")

    # -- tool gate (ACP client hook) --

    HOOK_RUN_METHOD = "_x.ai/hooks/run"
    HOOK_CALLBACK_ID = "agentry-tool-gate"

    def _hooks_meta(self):
        """`_meta` for session/new: one PreToolUse group matching every tool,
        answered by _hook_decision. Shape from grok-build
        extensions/hooks.rs (parse_hook_group)."""
        return {"x.ai/hooks": {"PreToolUse": [
            {"matcher": "*", "hookCallbackIds": [self.HOOK_CALLBACK_ID]}]}}

    @staticmethod
    def _canon(p):
        return os.path.normcase(os.path.realpath(p))

    def _path_allowed(self, p):
        if not isinstance(p, str) or not p:
            return False
        try:
            rp = self._canon(p)
        except Exception:
            return False
        return any(rp == root or rp.startswith(root + os.sep) for root in self._allowed_roots)

    def _hook_decision(self, params):
        """pre_tool_use verdict for one tool call. {} lets it run; a deny
        aborts it with the message shown to the model."""
        tool = params.get("toolName")
        inp = params.get("toolInput") or {}
        why = "may only be used on image files the user attached in this chat"
        if tool == "image_gen":
            return {}
        if tool == "read_file":
            ok = self._path_allowed(inp.get("target_file"))
        elif tool == "image_edit":
            ok = self._paths_allowed(inp.get("image"))
        elif tool in self._VIDEO_TOOLS:
            # Video only through /v1/videos (a video_turn session): the chat
            # contract has no slot for a clip, and a clip is a quota event.
            if params.get("sessionId") != self._video_sid:
                ok = False
                why = "is only available through the /v1/videos API"
            else:
                refs = [inp.get(k) for k in ("image", "first_frame", "last_frame",
                                              "images", "keyframes")]
                refs = [r for r in refs if r]
                ok = bool(refs) and all(self._paths_allowed(r) for r in refs)
        else:
            ok = False
        if ok:
            return {}
        _log(f"grok hook: denied {tool} {json.dumps(inp)[:160]}")
        return {"decision": "deny",
                "systemMessage": f"agentry: {tool or 'this tool'} {why}"}

    def _paths_allowed(self, paths):
        if isinstance(paths, str):
            paths = [paths]
        return bool(paths) and all(self._path_allowed(p) for p in paths)

    def _write_refs(self, images, subdir):
        """Write (mime, b64) attachments under refs/<subdir>/; returns their
        forward-slash paths (what the model sends back in tool calls)."""
        d = os.path.join(self.refs_root, subdir)
        os.makedirs(d, exist_ok=True)
        existing = len(os.listdir(d))
        paths = []
        for i, (mime, data) in enumerate(images, existing + 1):
            p = os.path.join(d, f"ref-{i}{self._REF_EXT.get(mime, '.img')}")
            with open(p, "wb") as f:
                f.write(base64.b64decode(data))
            paths.append(p.replace("\\", "/"))
        return paths

    def _stderr_loop(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                if line:
                    _log(f"grok stderr: {line.rstrip()[:200]}")
        except Exception:
            pass

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "agentry", "version": "0.2.0"},
        }, timeout=30)
        meta = result.get("_meta") or {}
        self._models = ((meta.get("modelState") or {}).get("availableModels")) or []
        caps = (result.get("agentCapabilities") or {}).get("promptCapabilities") or {}
        _log(f"grok initialized; agent={meta.get('agentVersion')!r} "
             f"models={[m.get('modelId') for m in self._models]} "
             f"image_input={caps.get('image')}")

    # -- selection --

    def _efforts_for(self, model):
        for m in self._models:
            if m.get("modelId") == model:
                ids = [e.get("id") for e in ((m.get("_meta") or {}).get("reasoningEfforts") or [])]
                return [e for e in ids if e]
        return []

    def _effort_for(self, model, effort):
        """agentry effort word -> what this grok model accepts (nearest lower
        rung when the exact level is missing, e.g. xhigh on grok-4.5)."""
        want = self.EFFORT_MAP.get(effort, effort)
        supported = self._efforts_for(model)
        if not supported or want in supported:
            return want
        if want not in self.EFFORT_LADDER:
            return supported[0]
        i = self.EFFORT_LADDER.index(want)
        for cand in reversed(self.EFFORT_LADDER[:i]):
            if cand in supported:
                return cand
        return supported[-1]

    def _apply_selection(self, sid, model, effort):
        """Set a session's model/effort when they differ from the request.
        Caller holds turn_lock (or owns a scratch session nobody else sees).
        Raises BackendError on rejection."""
        state = self._sess_state.setdefault(sid, [None, None])
        if model != state[0]:
            self._request("session/set_model",
                          {"sessionId": sid, "modelId": model}, timeout=15)
            state[0] = model
            # A model switch may reset effort on grok's side; re-apply below.
            state[1] = None
        if effort != state[1]:
            self._request("session/set_config_option",
                          {"sessionId": sid, "configId": "reasoning_effort",
                           "value": effort}, timeout=15)
            state[1] = effort

    def _open_session(self, cwd=None, model=None, effort=None):
        """session/new + selection; returns the id. Does NOT touch
        session_id — new_session() and image_turn() both build on this."""
        try:
            result = self._request("session/new", {
                "cwd": os.path.abspath(cwd) if cwd else self.cwd,
                "mcpServers": [],
                "_meta": self._hooks_meta()}, timeout=60)
        except BackendError as e:
            # Expired login (7-day token) surfaces here first.
            raise BackendError(f"{e}. If this is an auth error, run `grok login`.")
        sid = result.get("sessionId")
        if not sid:
            raise BackendError("session/new returned no sessionId")
        self._sess_state[sid] = [((result.get("models") or {}).get("currentModelId")), None]
        want_model = model or self.default_model
        self._apply_selection(sid, want_model,
                              self._effort_for(want_model, effort or self.default_effort))
        return sid

    def _close_session(self, sid):
        """session/close plus this session's attachment copies. Grok's own
        session record and generated images stay on disk."""
        self._sess_state.pop(sid, None)
        shutil.rmtree(os.path.join(self.refs_root, sid), ignore_errors=True)
        try:
            self._request("session/close", {"sessionId": sid}, timeout=10)
        except Exception as e:
            _log(f"grok session/close {sid}: {e}")

    # -- Backend interface --

    def new_session(self, cwd=None, model=None, effort=None):
        with self.turn_lock:
            old = self.session_id
            self.session_id = self._open_session(cwd, model, effort)
            self.session_fresh = True
            st = self._sess_state[self.session_id]
            _log(f"grok session: {self.session_id} (model={st[0]} effort={st[1]})")
            if old:
                self._close_session(old)
            return self.session_id

    def current_model(self):
        return self.default_model

    def list_models(self):
        out = []
        for m in self._models:
            mid = m.get("modelId")
            if not mid or mid in self.HIDDEN_MODELS:
                continue
            out.append({"id": mid, "name": m.get("name") or mid,
                        "displayName": m.get("name") or mid,
                        "isDefault": mid == self.default_model,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": e} for e in self._efforts_for(mid)]})
        return out or None

    def prompt(self, text, images=None, timeout=900, model=None, effort=None,
               session_id=None):
        """Generator for one turn on the chat session, or on `session_id`
        (grok-only extension used by image_turn's throwaway sessions).
        Serialized by the same turn lock either way."""
        sid = session_id or self.session_id
        if not sid:
            raise BackendError("no active session (call new_session first)")
        if images:
            # No image content over ACP: park the files under refs/<session>/
            # (kept for the session's life so later turns can still name
            # them) and tell the model where they are. The hook lets
            # read_file / image_edit touch exactly these.
            paths = self._write_refs(images, sid)
            _log(f"grok: {len(paths)} attachment(s) written under refs/{sid}")
            text = (f"[The user attached {len(paths)} image file(s) to this message: "
                    + ", ".join(paths) + ". Look at each with read_file before "
                    "answering. If the user wants a picture edited or made from "
                    "them, pass these paths to the image tool.]\n\n" + (text or ""))
        with self.turn_lock:
            want_model = model or self.default_model
            try:
                self._apply_selection(sid, want_model,
                                      self._effort_for(want_model, effort or self.default_effort))
            except BackendError as e:
                yield f"\n[grok error] {e}"
                return
            q = queue.Queue()
            self.active_turn_queue = q
            msg_id = self._next_id()
            # The prompt's RESPONSE ends the turn, so it lands in the same
            # queue as the notifications (dict vs tuple tells them apart).
            self.pending[msg_id] = q
            self._turn_in_flight = True
            self._active_sid = sid
            self._turn_usage = None
            tool_prompts = {}      # toolCallId -> prompt the model gave image_gen
            tool_names = {}        # toolCallId -> tool name
            t0 = time.time()
            try:
                self._write({"jsonrpc": "2.0", "id": msg_id, "method": "session/prompt",
                             "params": {"sessionId": sid,
                                        "prompt": [{"type": "text", "text": text}]}})
                if sid == self.session_id:
                    self.session_fresh = False
                while True:
                    try:
                        item = q.get(timeout=timeout)
                    except queue.Empty:
                        # finally: sends session/cancel for the abandoned turn
                        yield f"\n[grok timeout after {timeout}s]"
                        return
                    if isinstance(item, dict):           # the session/prompt response
                        self._turn_in_flight = False
                        if "error" in item:
                            err = item["error"]
                            msg = err.get("message", err) if isinstance(err, dict) else err
                            yield f"\n[grok error] {msg}"
                        else:
                            stop = (item.get("result") or {}).get("stopReason")
                            _log(f"grok turn stop={stop}" + self._usage_suffix())
                        return
                    method, params = item
                    if params.get("sessionId") not in (None, sid):
                        continue
                    upd = params.get("update") or {}
                    kind = upd.get("sessionUpdate")
                    if method == "session/update":
                        content = upd.get("content") or {}
                        if kind == "agent_message_chunk":
                            if content.get("type") == "text" and content.get("text"):
                                yield content["text"]
                        elif kind == "agent_thought_chunk":
                            if content.get("type") == "text" and content.get("text"):
                                yield ("reasoning", content["text"])
                        elif kind == "tool_call":
                            tid = upd.get("toolCallId")
                            name = (((upd.get("_meta") or {}).get("x.ai/tool") or {}).get("name")
                                    or upd.get("title"))
                            tool_names[tid] = name
                            raw = upd.get("rawInput") or {}
                            if name in self._IMAGE_TOOLS or name in self._VIDEO_TOOLS:
                                tool_prompts[tid] = raw.get("prompt")
                                self._image_t0 = time.time()
                                _log(f"grok {name} started"
                                     + (f" ({len(raw['image'])} reference(s))"
                                        if isinstance(raw.get("image"), list) else "")
                                     + (f" duration={raw.get('duration')} "
                                        f"{raw.get('resolution_name')}"
                                        if name in self._VIDEO_TOOLS else ""))
                            elif name == "read_file":
                                # Looking at an attachment; the hook has
                                # already vetted the path.
                                _log(f"grok read_file {str(raw.get('target_file'))[-40:]!r}")
                            else:
                                _log(f"WARN: grok called tool {name!r} despite the profile")
                        elif kind == "tool_call_update":
                            tid = upd.get("toolCallId")
                            name = tool_names.get(tid)
                            if name in self._VIDEO_TOOLS:
                                status = upd.get("status")
                                if status == "completed":
                                    yield self._video_from_update(upd, tool_prompts.get(tid))
                                elif status == "failed":
                                    yield f"\n[grok video error] {self._update_text(upd)[:300]}"
                                continue
                            if name not in self._IMAGE_TOOLS:
                                continue
                            status = upd.get("status")
                            if status == "completed":
                                yield self._image_from_update(upd, tool_prompts.get(tid))
                            elif status == "failed":
                                yield f"\n[grok image error] {self._update_text(upd)[:200]}"
                    elif method == "_x.ai/session_notification":
                        if kind == "turn_completed":
                            usage = upd.get("usage") or {}
                            self._turn_usage = usage
                            ticks = usage.get("costUsdTicks") or 0
                            self._cost_ticks += ticks
            finally:
                if self._turn_in_flight:
                    # Abandoned mid-turn (client disconnect / generator close):
                    # stop grok's work, don't let it stream into the next turn.
                    self._cancel_turn(sid)
                    self._turn_in_flight = False
                self._active_sid = None
                self.active_turn_queue = None
                self.pending.pop(msg_id, None)
                _log(f"grok turn took {time.time() - t0:.1f}s")

    _IMAGE_TOOLS = ("image_gen", "image_edit")
    _VIDEO_TOOLS = ("image_to_video", "reference_to_video")
    _REF_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/webp": ".webp"}

    def video_turn(self, prompt, image, seconds=6, size_text="", timeout=900,
                   model=None, effort=None):
        """One /v1/videos call: `image_to_video` from one reference image on
        a throwaway session. Grok has no text-only video (`reference_to_video`
        wants at least one image/frame/voice input), so a reference is
        mandatory. Probed 2026-10-05 on 1.0.46: 6 s is the tool's floor,
        480p the model's default pick, ~30 s per clip, MP4 H.264+AAC 448x672
        for a portrait reference, reported like images as a JSON blob with
        the file path under ~/.grok/sessions/<cwd>/<session>/videos/. Yields
        like prompt(): text, notes starting with "[", and one
        ("video", "video/mp4", path, revised_prompt) tuple on success."""
        sub = "vid-" + uuid.uuid4().hex
        paths = self._write_refs([image], sub)
        refs_dir = os.path.join(self.refs_root, sub)
        text = ("Call the image_to_video tool exactly once, with the file "
                f"{paths[0]} as its `image` argument and a duration of {int(seconds)} "
                "seconds. Pass the complete text below as its prompt, verbatim, "
                "including any size or resolution requirement. Then reply with "
                "one short sentence:\n\n" + prompt.rstrip()
                + (" " + size_text.strip() if size_text else ""))
        sid = None
        try:
            sid = self._open_session(model=model, effort=effort)
            self._video_sid = sid
            _log(f"grok video session: {sid}")
            yield from self.prompt(text, timeout=timeout, model=model, effort=effort,
                                   session_id=sid)
        finally:
            self._video_sid = None
            if sid:
                self._close_session(sid)
            shutil.rmtree(refs_dir, ignore_errors=True)

    def _video_from_update(self, upd, revised_prompt):
        """Completed image_to_video/reference_to_video update -> ("video",
        "video/mp4", path, prompt), or a visible error note. Returns the
        path, not the bytes: the Videos route serves the file on GET."""
        dt = time.time() - (getattr(self, "_image_t0", None) or time.time())
        text = self._update_text(upd)
        path = None
        try:
            path = json.loads(text).get("path")
        except Exception:
            pass
        if not path or not os.path.isfile(path):
            _log(f"grok video: no usable path after {dt:.1f}s: {text[:120]!r}")
            return f"\n[grok video error] {text[:200] or 'no video path reported'}"
        _log(f"grok video: {os.path.getsize(path) / 1024:.0f} KB in {dt:.1f}s (saved {path})")
        return ("video", "video/mp4", path, revised_prompt)

    def image_turn(self, prompt, images=None, timeout=900, model=None, effort=None):
        """One Images-API call on a throwaway ACP session (the chat session
        never sees the references or the result). The ACP prompt takes no
        image content, but grok's `image_edit` tool takes `image: [paths]`,
        so references are written under the scratch cwd and named in the
        instruction; `image_gen` is asked for when there are none. Probed
        2026-10-04 on 1.0.46: one card edited in 10 s, a two-reference scene
        in 15 s, both 832x1248 JPEG. Yields like prompt(): text, notes
        starting with "[", and ("image", ...) tuples."""
        refs_dir, paths = None, []
        if images:
            sub = "img-" + uuid.uuid4().hex
            paths = self._write_refs(images, sub)
            refs_dir = os.path.join(self.refs_root, sub)
        if paths:
            text = ("Call the image editing tool (image_edit) exactly once, with "
                    f"these {len(paths)} reference image file(s) as its `image` "
                    "argument, in this order: " + ", ".join(paths) + ". Pass the "
                    "complete text below as its prompt, verbatim, including any "
                    "size requirement. Keep the composition and aspect ratio of "
                    "the first reference unless the text below says otherwise. "
                    "Then reply with one short sentence:\n\n" + prompt)
        else:
            text = ("Call the image generation tool (image_gen) exactly once, "
                    "passing the complete text below as its prompt, verbatim, "
                    "including any size requirement. Then reply with one short "
                    "sentence:\n\n" + prompt)
        sid = None
        try:
            sid = self._open_session(model=model, effort=effort)
            _log(f"grok scratch session: {sid} ({len(paths)} reference file(s))")
            yield from self.prompt(text, timeout=timeout, model=model, effort=effort,
                                   session_id=sid)
        finally:
            if sid:
                self._close_session(sid)
            if refs_dir:
                # Our copies of the client's upload; the generated image stays
                # under ~/.grok/sessions with the rest of the session.
                shutil.rmtree(refs_dir, ignore_errors=True)

    @staticmethod
    def _update_text(upd):
        parts = []
        for c in upd.get("content") or []:
            inner = c.get("content") if isinstance(c, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "text":
                parts.append(inner.get("text") or "")
        return "".join(parts)

    def _image_from_update(self, upd, revised_prompt):
        """Completed image_gen/image_edit tool_call_update -> ("image", mime,
        b64, prompt), or a visible error note. The tool reports a JSON blob
        with the path of the file it wrote (JPEG today), not the bytes."""
        dt = time.time() - (getattr(self, "_image_t0", None) or time.time())
        text = self._update_text(upd)
        path = None
        try:
            info = json.loads(text)
            path = info.get("path")
        except Exception:
            pass
        if not path:
            _log(f"grok image: no path in tool result after {dt:.1f}s: {text[:120]!r}")
            return f"\n[grok image error] {text[:200] or 'no image path reported'}"
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            _log(f"grok image: cannot read {path}: {e}")
            return f"\n[grok image error] cannot read {path}"
        mime = next((m for magic, m in self._IMAGE_MAGIC if data.startswith(magic)),
                    "image/jpeg")
        _log(f"grok image: {len(data) / 1024:.0f} KB {mime} in {dt:.1f}s (saved {path})")
        return ("image", mime, base64.b64encode(data).decode("ascii"), revised_prompt)

    def _cancel_turn(self, sid):
        try:
            self._notify("session/cancel", {"sessionId": sid})
            return True
        except Exception:
            return False

    def cancel(self):
        sid = self._active_sid
        if not (self._turn_in_flight and sid):
            return False
        return self._cancel_turn(sid)

    def _usage_suffix(self):
        u = self._turn_usage
        if not u:
            return ""
        cost = (u.get("costUsdTicks") or 0) / 1e10
        return (f"  tokens in={u.get('inputTokens', 0)} (cached {u.get('cachedReadTokens', 0)}) "
                f"out={u.get('outputTokens', 0)} reasoning={u.get('reasoningTokens', 0)}"
                f"  ~${cost:.4f} (run ~${self._cost_ticks / 1e10:.3f})")

    def quota_status(self):
        """Grok exposes no subscription quota; the only metering is the
        per-turn cost estimate grok itself reports (model tokens only, the
        image tool's cost is not in it)."""
        if not self._cost_ticks:
            return None
        return (f"grok usage | this run ~${self._cost_ticks / 1e10:.3f} est. "
                f"(tokens only, images excluded; no subscription quota readout)")

    def is_alive(self):
        return self.proc.poll() is None

    def close(self):
        if self.session_id:
            shutil.rmtree(os.path.join(self.refs_root, self.session_id), ignore_errors=True)
        # EOF on stdin lets grok flush its session persistence; terminate
        # only if it lingers.
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
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


def make_backend(kind, *, model=None, reasoning_effort=None, log_dir: Optional[Path] = None) -> Backend:
    """Construct the selected backend. `model`/`reasoning_effort` None means
    'use the backend's own default'."""
    log_dir = log_dir or (Path(__file__).parent / "logs")
    if kind == "copilot":
        return CopilotSDKBackend(
            model=model,
            reasoning_effort=reasoning_effort,
            log_path=log_dir / "copilot_sdk.log",
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
    if kind == "grok":
        kw = {"log_path": log_dir / "grok_wire.log"}
        if model is not None:
            kw["model"] = model
        if reasoning_effort is not None:
            kw["reasoning_effort"] = reasoning_effort
        return GrokACPBackend(**kw)
    raise BackendError(f"unknown backend {kind!r} (expected 'copilot', 'codex', 'claude', or 'grok')")
