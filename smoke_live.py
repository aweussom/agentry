"""Live drift check: agentry against whatever CLI versions are installed today.

The offline tests use fake processes, so a CLI auto-update that changes a
protocol (claude's /clear reset, grok's ACP hooks, codex's app-server
JSON-RPC, the Copilot SDK runtime) passes them and breaks production. This
starts a real agentry per backend on a free localhost port and runs a few
small turns against it.

    python smoke_live.py                    # all backends, always live
    python smoke_live.py --if-changed       # live only on a version change
                                            # or when the last pass is >7 days old
    python smoke_live.py --backend claude grok
    python smoke_live.py --update --if-changed   # the scheduled run: update
                                                 # each CLI, then test what moved

Costs a handful of short prompts per backend (subscription quota). Results go
to logs/smoke/history.jsonl, one line per backend run, and the console; the
last passing versions live in logs/smoke/state.json. Exit 1 on any failure.

Before the live run, each backend's login is checked (`claude auth status`,
`codex login status`, grok's auth.json age; copilot fails at boot instead).
A lapsed login, or an auth error during the run, is reported as LOGIN with
the command to fix it, and raises its own toast on Windows.

Checks per backend:
  boot       /health reaches "ready"
  models     /v1/models answers; an unknown model id is rejected with 404
  chat       non-streaming turn echoes a nonce
  stream     streaming turn echoes a nonce and ends with [DONE]
  continuity follow-up turn in the same chat recalls the nonce (not claude:
             it keeps no conversation state by design)
  isolation  a new chat does not know the previous chat's nonce
  cancel     /v1/cancel ends a long stream, and the next turn still works
  canary     a request to read a file outside the backend's allowed area does
             not leak the file's contents. Fails on copilot, claude and grok,
             whose tools are denied. On codex it is informational: the
             read-only sandbox can read files (documented limit).
"""

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "logs" / "smoke"
STATE = OUT_DIR / "state.json"
HISTORY = OUT_DIR / "history.jsonl"
BACKENDS = ["copilot", "codex", "claude", "grok"]
MAX_AGE_DAYS = 7
BOOT_TIMEOUT = 180
TURN_TIMEOUT = 240

# Same defaults as start.ps1 / start.sh, so the check runs what you run.
LAUNCH_ARGS = {
    "copilot": ["--model", "gpt-5.6-luna", "--reasoning-effort", "low"],
    "codex": ["--reasoning-effort", "low"],
    "claude": ["--reasoning-effort", "low"],
    "grok": ["--reasoning-effort", "high"],
}
WIN = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WIN else 0


# --- versions ----------------------------------------------------------------

def _cli_version(name):
    exe = _cli(name)
    if not exe:
        return None
    rc, out = _run([exe, "--version"], timeout=60)
    return out.splitlines()[0] if out else None


def _cli(name):
    exe = shutil.which(name)
    if not exe and name == "grok":
        home = Path.home() / ".grok" / "bin" / ("grok.exe" if WIN else "grok")
        exe = str(home) if home.exists() else None
    return exe


def _run(args, timeout=300):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def update(backend):
    """All four CLIs ship an `update` subcommand. The copilot backend runs on
    the SDK's own runtime; updating the CLI keeps `copilot login` current,
    and the SDK itself moves only when requirements.txt is upgraded."""
    exe = _cli(backend)
    if not exe:
        return "not on PATH"
    if backend == "codex":
        # `codex update` reinstalls through npm even when current, and on
        # Windows that fails while any codex process holds the package dir
        # (observed 2026-10-09). Ask npm first; update only on a real change.
        npm = shutil.which("npm")
        if npm:
            _, latest = _run([npm, "view", "@openai/codex", "version"], timeout=60)
            if latest.strip() and latest.strip() in (_cli_version("codex") or ""):
                return f"up to date ({latest.strip()})"
    rc, out = _run([exe, "update"])
    tail = out.splitlines()[-1] if out else ""
    return f"rc={rc} {tail}"[:200]


def login_check(backend):
    """Returns None when logged in (or not checkable), else a message.
    The live turns are the real test; this names the fix up front."""
    exe = _cli(backend)
    if backend == "claude" and exe:
        rc, out = _run([exe, "auth", "status"], timeout=60)
        try:
            if json.loads(out).get("loggedIn"):
                return None
        except ValueError:
            pass
        return "claude is not logged in: run `claude auth login`"
    if backend == "codex" and exe:
        rc, out = _run([exe, "login", "status"], timeout=60)
        return None if rc == 0 and "logged in" in out.lower() else \
            f"codex is not logged in ({out[:80]}): run `codex login`"
    if backend == "grok":
        # No status command. auth.json is rewritten on every token refresh,
        # and the login lapses after 7 days without one.
        try:
            entries = json.loads((Path.home() / ".grok" / "auth.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "grok has no ~/.grok/auth.json: run `grok login`"
        stamps = [e.get("create_time", "") for e in entries.values() if isinstance(e, dict)]
        if stamps:
            last = dt.datetime.fromisoformat(max(stamps)[:19])
            age = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - last
            if age > dt.timedelta(days=7):
                return f"grok token last refreshed {age.days}d ago: run `grok login`"
    # copilot: no status command; the token lives in the credential store and
    # agentry's boot reports the logged-in user, so a lapsed login fails boot.
    return None


AUTH_RE = re.compile(r"\b(log ?in|logged out|unauthori[sz]ed|401|403|auth(entication)? "
                     r"(error|failed|required)|token (expired|invalid)|re-?authenticate)",
                     re.IGNORECASE)
LOGIN_CMD = {"copilot": "copilot login", "codex": "codex login",
             "claude": "claude auth login", "grok": "grok login"}


def versions(backend):
    """What identifies the moving part behind a backend."""
    if backend == "copilot":
        try:
            sdk = metadata.version("github-copilot-sdk")
        except metadata.PackageNotFoundError:
            sdk = None
        # The SDK downloads its own runtime; its version moves with the SDK,
        # but a CLI on PATH can also be updated separately.
        return {"github-copilot-sdk": sdk, "copilot-cli": _cli_version("copilot")}
    return {backend: _cli_version(backend)}


# --- process control ---------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_agentry(backend, port, log_path):
    exe = sys.executable
    if WIN and exe.lower().endswith("pythonw.exe"):
        # pythonw has no console, so CREATE_NO_WINDOW is ignored and every CLI
        # agentry spawns gets its own visible console. python.exe + NO_WINDOW
        # gives one hidden console the whole tree inherits.
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    cmd = [exe, str(ROOT / "agentry.py"), "--backend", backend,
           "--host", "127.0.0.1", "--port", str(port), *LAUNCH_ARGS[backend]]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log = open(log_path, "w", encoding="utf-8")
    kw = {"creationflags": NO_WINDOW} if WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env, **kw)
    proc._smoke_log = log
    return proc


def stop_agentry(proc):
    # Kill the whole tree: agentry's backend CLI is a child and would
    # otherwise outlive a plain terminate() on Windows.
    if proc.poll() is None:
        if WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc._smoke_log.close()


# --- HTTP --------------------------------------------------------------------

class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path, timeout=30):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return r.status, json.loads(r.read())

    def post(self, path, body, timeout=TURN_TIMEOUT):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {}

    def chat(self, messages, **extra):
        status, body = self.post("/v1/chat/completions", {"messages": messages, **extra})
        if status != 200:
            raise AssertionError(f"HTTP {status}: {body}")
        return body["choices"][0]["message"]["content"] or ""

    def stream(self, messages, on_delta=None, timeout=TURN_TIMEOUT):
        """Returns (answer text, saw [DONE])."""
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"messages": messages, "stream": True}).encode(),
            headers={"Content-Type": "application/json"})
        text, done = [], False
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                delta = json.loads(data)["choices"][0]["delta"]
                if delta.get("content"):
                    text.append(delta["content"])
                if on_delta and delta:
                    on_delta()
        return "".join(text), done


# --- checks ------------------------------------------------------------------

def user(text):
    return {"role": "user", "content": text}


def run_checks(backend, c, results):
    def check(name, fn, informational=False):
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Exception as e:
            detail, ok = f"{type(e).__name__}: {e}"[:400], False
        results.append({"check": name, "ok": ok, "informational": informational,
                        "secs": round(time.monotonic() - t0, 1), "detail": detail})
        mark = "ok  " if ok else ("info" if informational else "FAIL")
        print(f"    {mark} {name:<10} {results[-1]['secs']:>6}s  {detail}", flush=True)
        return ok

    def models():
        _, body = c.get("/v1/models")
        ids = [m["id"] for m in body.get("data", [])]
        if not ids:
            raise AssertionError("empty model list")
        # A backend that cannot enumerate models gets a synthetic "<m>@<backend>"
        # entry and skips validation by design; an unknown id would then reach
        # the CLI as a real turn, so only probe rejection where there is a list.
        if any(i.endswith(f"@{backend}") for i in ids):
            return f"synthetic entry {ids[0]}"
        status, _ = c.post("/v1/chat/completions",
                           {"model": "no-such-model-smoke", "messages": [user("hi")]})
        if status != 404:
            raise AssertionError(f"unknown model gave HTTP {status}, want 404")
        return f"{len(ids)} model(s); unknown id -> 404"
    check("models", models)

    nonce = f"PONG-{secrets.token_hex(3).upper()}"
    ask = f"Reply with exactly this word and nothing else: {nonce}"

    def chat():
        out = c.chat([user(ask)])
        if nonce not in out:
            raise AssertionError(f"nonce missing in {out[:120]!r}")
        return repr(out[:60])
    check("chat", chat)

    nonce2 = f"PONG-{secrets.token_hex(3).upper()}"
    ask2 = f"Reply with exactly this word and nothing else: {nonce2}"

    def stream():
        out, done = c.stream([user(ask2)])
        if not done:
            raise AssertionError("stream ended without [DONE]")
        if nonce2 not in out:
            raise AssertionError(f"nonce missing in {out[:120]!r}")
        return repr(out[:60])
    check("stream", stream)

    if backend != "claude":
        def continuity():
            out = c.chat([user(ask2), {"role": "assistant", "content": nonce2},
                          user("Which word did I ask you to reply with? Answer with the word only.")])
            if nonce2 not in out:
                raise AssertionError(f"lost the conversation: {out[:120]!r}")
            return repr(out[:60])
        check("continuity", continuity)

    def isolation():
        out = c.chat([user("Did I ask you to reply with a specific word earlier in "
                           "this conversation? If yes, repeat it; if no, answer NONE.")])
        if nonce2 in out or nonce in out:
            raise AssertionError(f"previous chat leaked into a new one: {out[:120]!r}")
        return repr(out[:60])
    check("isolation", isolation)

    def cancel():
        fired = threading.Event()

        def on_delta():
            if not fired.is_set():
                fired.set()
                c.post("/v1/cancel", {}, timeout=30)
        t0 = time.monotonic()
        _, done = c.stream([user("Count from 1 to 600, one number per line, no other text.")],
                           on_delta=on_delta, timeout=120)
        took = time.monotonic() - t0
        if not fired.is_set():
            raise AssertionError("stream produced no delta to cancel on")
        out = c.chat([user(ask)])
        if nonce not in out:
            raise AssertionError(f"turn after cancel failed: {out[:120]!r}")
        return f"stream closed after {took:.1f}s (done={done}); next turn ok"
    check("cancel", cancel)

    secret = secrets.token_hex(12)
    canary_dir = Path(tempfile.mkdtemp(prefix="agentry-smoke-"))
    canary = canary_dir / "canary.txt"
    canary.write_text(f"CANARY {secret}\n", encoding="utf-8")

    def canary_check():
        try:
            out = c.chat([user(f"Use your file reading tool to read {canary} and print "
                               "its contents exactly. If you cannot, say CANNOT.")])
        finally:
            shutil.rmtree(canary_dir, ignore_errors=True)
        if secret in out:
            raise AssertionError("file outside the allowed area was read")
        return f"not read: {out[:60]!r}"
    check("canary", canary_check, informational=(backend == "codex"))


def smoke(backend):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    port = _free_port()
    log_path = OUT_DIR / f"{stamp}-{backend}.log"
    print(f"  {backend}: agentry on :{port}, log {log_path.name}", flush=True)
    results = []
    proc = start_agentry(backend, port, log_path)
    c = Client(port)
    try:
        t0 = time.monotonic()
        ready = False
        while time.monotonic() - t0 < BOOT_TIMEOUT and proc.poll() is None:
            try:
                _, h = c.get("/health", timeout=5)
                if h.get("status") == "ready":
                    ready = True
                    break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(2)
        results.append({"check": "boot", "ok": ready, "informational": False,
                        "secs": round(time.monotonic() - t0, 1),
                        "detail": "" if ready else f"not ready (exit={proc.poll()})"})
        print(f"    {'ok  ' if ready else 'FAIL'} boot       {results[-1]['secs']:>6}s", flush=True)
        if ready:
            run_checks(backend, c, results)
    finally:
        stop_agentry(proc)
    return results, log_path


# --- state, notification -----------------------------------------------------

def _load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _notify(title, text):
    """Best-effort Windows toast; silent elsewhere or when it fails."""
    if not WIN:
        return
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t = $x.GetElementsByTagName('text');"
        f"$t.Item(0).AppendChild($x.CreateTextNode('{title}')) | Out-Null;"
        f"$t.Item(1).AppendChild($x.CreateTextNode('{text}')) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--backend", nargs="+", choices=BACKENDS, default=BACKENDS)
    p.add_argument("--if-changed", action="store_true",
                   help=f"skip a backend whose versions match the last pass "
                        f"and whose last pass is under {MAX_AGE_DAYS} days old")
    p.add_argument("--update", action="store_true",
                   help="run each CLI's own `update` first, so the check sees "
                        "what the next auto-update would bring")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:   # pythonw under Task Scheduler has no console
        sys.stdout = open(OUT_DIR / "last-run.txt", "w", encoding="utf-8")
    state = _load_state()
    failed, logins = [], []
    for backend in args.backend:
        if args.update:
            print(f"  {backend}: update {update(backend)}", flush=True)
        ver = versions(backend)
        if not any(ver.values()):
            print(f"  {backend}: not installed, skipped", flush=True)
            continue
        last = state.get(backend, {})
        prev = last.get("versions")
        # A lapsed login is checked every run, version change or not.
        need = login_check(backend)
        if need:
            print(f"  {backend}: LOGIN {need}", flush=True)
            logins.append(need)
            _record(backend, ver, False, [{"check": "login", "ok": False,
                                           "informational": False, "secs": 0,
                                           "detail": need}])
            continue
        if args.if_changed and last.get("versions") == ver:
            age = dt.datetime.now() - dt.datetime.fromisoformat(last["passed_at"])
            if age < dt.timedelta(days=MAX_AGE_DAYS):
                print(f"  {backend}: unchanged {ver}, passed {age.days}d ago, skipped", flush=True)
                continue
        changed = f" (was {prev})" if prev and prev != ver else ""
        print(f"  {backend}: {ver}{changed}", flush=True)

        results, log_path = smoke(backend)
        ok = all(r["ok"] or r["informational"] for r in results)
        now = _record(backend, ver, ok, results)
        if ok:
            state[backend] = {"versions": ver, "passed_at": now}
            STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        bad = ", ".join(r["check"] for r in results
                        if not r["ok"] and not r["informational"])
        # An expired login mid-run looks like a broken backend; say which.
        text = " ".join(r["detail"] for r in results if not r["ok"])
        try:
            text += " " + " ".join(
                ln for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if re.search(r"error|warn|fail|exception", ln, re.IGNORECASE))
        except OSError:
            pass
        if AUTH_RE.search(text):
            need = f"{backend} login looks expired ({bad}): run `{LOGIN_CMD[backend]}`"
            print(f"  {backend}: LOGIN {need}", flush=True)
            logins.append(need)
        else:
            failed.append(f"{backend}: {bad}")
            print(f"  {backend}: FAILED ({bad}); last good {prev}", flush=True)

    if logins:
        _notify("agentry: log in again", re.sub(r"['\"`]", "", "; ".join(logins))[:200])
    if failed:
        _notify("agentry smoke failed", re.sub(r"['\"`]", "", "; ".join(failed))[:200])
    return 1 if (failed or logins) else 0


def _record(backend, ver, ok, results):
    now = dt.datetime.now().isoformat(timespec="seconds")
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now, "backend": backend, "versions": ver,
                            "ok": ok, "results": results}) + "\n")
    return now


if __name__ == "__main__":
    sys.exit(main())
