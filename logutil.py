"""Shared logging primitives for agentry and its backends.

Lives in its own module so the Flask app (agentry.py) and the backend
implementations (backends.py) write through the same elapsed-time clock and the
same console state without a circular import.

Console behaviour:
- Every real log line is timestamped and prefixed with elapsed time since the
  current request started (REQ_T0, stamped per thread by the HTTP handler).
- When the server is idle, a single *transient* keepalive line is rewritten in
  place once per interval (carriage-return, no ANSI — portable to conhost and
  Windows Terminal), so idle ticks don't scroll the console.
- Every Nth keepalive, a registered status provider (e.g. backend quota) is
  printed as a *permanent* line above the transient ticker — ordinary idle
  ticks update their own bottom line and never push these upward.
- Real log lines clear the transient ticker, print above it, and the ticker
  redraws on its next tick. On a non-TTY (redirected to a file) the transient
  ticker is suppressed entirely; only permanent lines are written.
"""
import datetime
import sys
import threading
import time

# tid -> request start time (monotonic). Set by the HTTP handler, read by log().
REQ_T0 = {}

_last_activity = time.monotonic()   # bumped only by real log lines, not keepalive
_print_lock = threading.Lock()
_status_provider = None             # callable -> Optional[str] for the Nth keepalive
_isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())

# Transient-line state (guarded by _print_lock):
_live_active = False                # a no-newline ticker line is currently on screen
_live_len = 0                       # its length, for blanking remnants


def now():
    return time.monotonic()


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_status_provider(fn):
    """Register a callable returning a short status string (or None) shown on
    every Nth keepalive. agentry wires this to the active backend's quota."""
    global _status_provider
    _status_provider = fn


def _write_permanent(line):
    """Caller holds _print_lock. Clears any transient ticker, prints a normal line."""
    global _live_active, _live_len
    if _live_active:
        sys.stdout.write("\r" + " " * _live_len + "\r")
        _live_active = False
        _live_len = 0
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _write_transient(line):
    """Caller holds _print_lock. Rewrites the in-place ticker line (TTY only)."""
    global _live_active, _live_len
    pad = max(_live_len, len(line))
    sys.stdout.write("\r" + line.ljust(pad))
    sys.stdout.flush()
    _live_active = True
    _live_len = len(line)


def log(msg):
    global _last_activity
    t0 = REQ_T0.get(threading.get_ident())
    elapsed = (now() - t0) if t0 else 0.0
    line = f"[{_stamp()}] [t={elapsed:6.2f}s] {msg}"
    with _print_lock:
        _last_activity = now()
        _write_permanent(line)


def _keepalive_loop(interval, every_n):
    tick = 0
    while True:
        time.sleep(interval)
        if now() - _last_activity < interval:
            tick = 0            # activity happened; restart the idle cadence
            continue
        tick += 1
        status = None
        if every_n and tick % every_n == 0 and _status_provider is not None:
            try:
                status = _status_provider()
            except Exception:
                status = None
        with _print_lock:
            if status:
                _write_permanent(f"[{_stamp()}] [keepalive] {status}")
            if _isatty:
                _write_transient(f"[{_stamp()}] [keepalive] idle")
            # non-TTY + no status: stay silent (don't spam the redirected log).


def start_keepalive(interval=60, every_n=10):
    """Start the idle-heartbeat thread. every_n keepalives show the status
    provider (e.g. backend quota) as a permanent line."""
    threading.Thread(target=_keepalive_loop, args=(interval, every_n),
                     daemon=True, name="keepalive").start()
