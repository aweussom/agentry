"""Regression test for audit A02: a new chat's session reset and its turn
must be one atomic operation.

The stub copies the Copilot backend's locking: new_session() swaps the
session under turn_lock (the old one is gone while the new one is created),
and prompt() checks for a session before it takes the lock. Concurrent new
chats used to fail in two ways:
  - a later request's reset disconnected an earlier request's fresh session
    before that request's turn started ("no active session", HTTP 500;
    seen live 2026-11-24 from news-collector's two-thread rewrite);
  - two requests ran their turns in the same session, so one could see the
    other's context.
Each new chat must get a 200 and a session of its own.

Run:  venv\\Scripts\\python test_session_reset_race.py
"""
import itertools
import threading
import time

import agentry
from backends import Backend, BackendError


class SwappingStub(Backend):
    def __init__(self):
        self.default_model = "stub-default"
        self.session_id = None
        self.session_fresh = False
        self.turn_lock = threading.Lock()
        self._session = None
        self._ids = itertools.count(1)
        self.turns = []            # session id each turn ran in

    def new_session(self, cwd=None, model=None, effort=None):
        with self.turn_lock:
            self._session = None   # old session disconnected first
            time.sleep(0.05)       # create_session round-trip
            self._session = f"s{next(self._ids)}"
            self.session_id = self._session
            self.session_fresh = True
            return self.session_id

    def prompt(self, text, images=None, timeout=900, model=None, effort=None):
        if not self._session:
            raise BackendError("no active session (call new_session first)")
        with self.turn_lock:
            sid = self._session
            self.session_fresh = False
            time.sleep(0.05)
            self.turns.append(sid)
            yield f"ran in {sid}"

    def cancel(self):
        return False

    def is_alive(self):
        return True

    def close(self):
        pass

    def list_models(self):
        return [{"id": "stub-default"}]


def _post(i, results, stream):
    client = agentry.app.test_client()
    body = {"messages": [{"role": "user", "content": f"story {i}"}],
            "stream": stream}
    try:
        resp = client.post("/v1/chat/completions", json=body)
        results[i] = (resp.status_code, resp.get_data(as_text=True))
    except Exception as e:     # the handler raising is the bug, not a test error
        results[i] = (500, repr(e))


def run(n, stream):
    stub = agentry._backend = SwappingStub()
    agentry.BACKEND_KIND = "stub"
    results = {}
    threads = [threading.Thread(target=_post, args=(i, results, stream))
               for i in range(n)]
    for t in threads:
        t.start()
        time.sleep(0.01)
    for t in threads:
        t.join()
    bad = {i: r for i, r in results.items() if r[0] != 200}
    assert not bad, f"stream={stream}: failed requests {bad}"
    assert len(stub.turns) == n, (stream, stub.turns)
    assert len(set(stub.turns)) == n, f"stream={stream}: shared sessions {stub.turns}"


def lock_is_free():
    if agentry._chat_lock.acquire(timeout=1):
        agentry._chat_lock.release()
        return True
    return False


def no_leaks():
    """The chat lock must come back on every exit path, or agentry hangs."""
    agentry._backend = SwappingStub()
    client = agentry.app.test_client()
    body = {"messages": [{"role": "user", "content": "x"}], "stream": True}
    # Streamed response closed by the client before it read anything.
    resp = client.post("/v1/chat/completions", json=body, buffered=False)
    resp.close()
    assert lock_is_free(), "lock leaked: stream closed unread"
    # Streamed response closed after the first chunk.
    resp = client.post("/v1/chat/completions", json=body, buffered=False)
    next(iter(resp.response))
    resp.close()
    assert lock_is_free(), "lock leaked: stream closed mid-read"

    class FailingReset(SwappingStub):
        def new_session(self, cwd=None, model=None, effort=None):
            raise BackendError("create_session failed")
    agentry._backend = FailingReset()
    resp = client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 500, resp.status_code
    assert lock_is_free(), "lock leaked: new_session failed"


def main():
    agentry.BACKEND_KIND = "stub"
    for stream in (False, True):
        for _ in range(5):
            run(4, stream)
    no_leaks()
    print("PASS: each concurrent new chat ran in its own session; no lock leaks")


if __name__ == "__main__":
    main()
