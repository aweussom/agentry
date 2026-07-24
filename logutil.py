"""Shared logging primitives for agentry and its backends.

Lives in its own module so the Flask app (agentry.py) and the backend
implementations (backends.py) write through the same elapsed-time clock and the
same console state without a circular import.

Console behaviour:
- Every real log line is timestamped and prefixed with elapsed time since the
  current request started (REQ_T0, stamped per thread by the HTTP handler).
- When the server is idle, a *transient* heartbeat line pulses '...*...*...*'
  in place once a second (carriage-return, no ANSI — portable to conhost and
  Windows Terminal), so the console shows liveness without scrolling.
- Periodically (snapshot_interval) a registered status provider (e.g. backend
  quota) is printed as a *permanent* line above the heartbeat — these stay in
  scrollback; the pulsing line never pushes them up.
- Real log lines clear the transient heartbeat, print above it, and the pulse
  resumes on its next tick. On a non-TTY (redirected to a file) the heartbeat
  is suppressed entirely; only permanent lines are written.
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
_ticker_provider = None             # callable -> Optional[str]: in-flight turn text
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


def set_ticker_provider(fn):
    """Register a callable returning the current line of in-flight turn output
    (or None when no turn is running). While it returns text, the heartbeat
    line becomes a news ticker scrolling that line; when the backend moves on
    to a new line, the ticker restarts on the new one."""
    global _ticker_provider
    _ticker_provider = fn


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


def _pulse(frame, width=11):
    """A scrolling '...*...*...*' heartbeat field; the stars sweep as frame++."""
    unit = "...*"
    base = unit * (width // len(unit) + 2)
    off = frame % len(unit)
    return base[off:off + width]


# Marquee state (only touched from the keepalive thread):
_tick_text = ""
_tick_off = 0


def _marquee(text, width=70, step=3):
    """One-line news ticker over `text`: lines longer than the window scroll
    left `step` chars per tick and wrap around with a '···' seam; new text
    (the backend moved to the next line) restarts from its beginning."""
    global _tick_text, _tick_off
    text = " ".join(text.split())       # collapse newlines/runs; strip controls
    if text != _tick_text:
        _tick_text, _tick_off = text, 0
    if len(text) <= width:
        return text.ljust(width)
    s = text + "   ···   "
    view = (s + s)[_tick_off % len(s):][:width]
    _tick_off += step
    return view


def _keepalive_loop(pulse_interval, snapshot_interval, idle_after):
    frame = 0
    last_snapshot = 0.0
    while True:
        time.sleep(pulse_interval)
        # A turn in flight takes over the transient line as a text ticker,
        # even inside the idle_after window (real log lines are sparse
        # mid-turn, and watching the model's words beats watching stars).
        tick = None
        if _ticker_provider is not None:
            try:
                tick = _ticker_provider()
            except Exception:
                tick = None
        if tick is None and now() - _last_activity < idle_after:
            frame = 0           # active again; pause the heartbeat
            continue
        status = None
        if _status_provider is not None:
            try:
                status = _status_provider()
            except Exception:
                status = None
        frame += 1
        with _print_lock:
            # Drop a permanent quota "keepalive" line into scrollback when going
            # idle and every snapshot_interval after — only when there's real
            # status (don't litter logs with periodic "idle").
            if status and (now() - last_snapshot) >= snapshot_interval:
                _write_permanent(f"[{_stamp()}] [keepalive] {status}")
                last_snapshot = now()
            # Between snapshots, pulse a live heartbeat line in place (TTY
            # only) — or, mid-turn, scroll the model's current output line.
            if _isatty:
                if tick:
                    _write_transient(f"[{_stamp()}] » {_marquee(tick)}")
                else:
                    _write_transient(f"[{_stamp()}] {_pulse(frame)}")
            # non-TTY: no heartbeat; only the periodic permanent line above.


def start_keepalive(pulse_interval=1.0, snapshot_interval=600.0, idle_after=3.0):
    """Start the idle heartbeat thread. When idle it pulses a '...*...*...*'
    line in place (TTY) once per pulse_interval, and drops a permanent status
    snapshot (e.g. backend quota) into scrollback every snapshot_interval."""
    threading.Thread(target=_keepalive_loop,
                     args=(pulse_interval, snapshot_interval, idle_after),
                     daemon=True, name="heartbeat").start()
