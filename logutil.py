"""Shared logging primitives for agentry and its backends.

Lives in its own module so the Flask app (agentry.py) and the backend
implementations (backends.py) write through the same elapsed-time clock and
the same keepalive heartbeat without a circular import.

Every line is prefixed with a wall-clock timestamp and the elapsed time since
the current request started (REQ_T0, stamped per thread by the HTTP handler).
When the server sits idle, start_keepalive() emits a heartbeat once per
interval so the console shows the process is alive.
"""
import datetime
import threading
import time

# tid -> request start time (monotonic). Set by the HTTP handler, read by log().
REQ_T0 = {}

# Monotonic time of the last emitted line; the keepalive uses it to detect idle.
_last_activity = time.monotonic()
_print_lock = threading.Lock()


def now():
    return time.monotonic()


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(line):
    """Serialize all console output through one lock and refresh the idle clock."""
    global _last_activity
    with _print_lock:
        _last_activity = now()
        print(line, flush=True)


def log(msg):
    t0 = REQ_T0.get(threading.get_ident())
    elapsed = (now() - t0) if t0 else 0.0
    _emit(f"[{_stamp()}] [t={elapsed:6.2f}s] {msg}")


def _keepalive_loop(interval):
    while True:
        time.sleep(interval)
        # Only beat when nothing has been logged for a full interval, so active
        # turns never get interleaved keepalive noise.
        if now() - _last_activity >= interval:
            # Bypass _emit so the heartbeat does NOT count as activity — that
            # keeps it firing every interval while the server stays idle.
            with _print_lock:
                print(f"[{_stamp()}] [keepalive] idle", flush=True)


def start_keepalive(interval=60):
    """Start the idle-heartbeat thread. Idempotent-safe to call once at boot."""
    threading.Thread(target=_keepalive_loop, args=(interval,),
                     daemon=True, name="keepalive").start()
