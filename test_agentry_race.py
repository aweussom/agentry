"""Regression test: per-request model/effort are request-isolated.

Two concurrent requests asking for different models must each run their turn
on the model they asked for (the selection-attribution race flagged in the
review of 6d2bdd6). Exercises the real Flask handler over a stub backend —
no agent runtime needed.

Run:  venv\\Scripts\\python test_agentry_race.py
"""
import threading
import time

import agentry
from backends import Backend


class StubBackend(Backend):
    def __init__(self):
        self.default_model = "stub-default"
        self.session_id = None
        self.session_fresh = False
        self.turn_lock = threading.Lock()
        self.turns = []            # (model, effort) as resolved per turn

    def new_session(self, cwd=None, model=None, effort=None):
        self.session_id = "stub-session"
        self.session_fresh = True
        return self.session_id

    def prompt(self, text, images=None, timeout=900, model=None, effort=None):
        with self.turn_lock:
            resolved = (model or self.default_model, effort)
            time.sleep(0.05)       # widen the window a racing writer would need
            self.turns.append(resolved)
            self.session_fresh = False
            yield f"ran:{resolved[0]}"

    def cancel(self):
        return False

    def is_alive(self):
        return True

    def close(self):
        pass

    def list_models(self):
        return [{"id": "model-a"}, {"id": "model-b"}, {"id": "stub-default"}]


def _post(model, results):
    client = agentry.app.test_client()
    body = {"messages": [{"role": "user", "content": f"hi from {model}"}]}
    if model:
        body["model"] = model
    resp = client.post("/v1/chat/completions", json=body)
    results[model] = (resp.status_code, resp.get_json())


def main():
    stub = agentry._backend = StubBackend()
    agentry.BACKEND_KIND = "stub"

    # Concurrent requests for different models: each must run and be labeled
    # with its own selection.
    results = {}
    threads = [threading.Thread(target=_post, args=(m, results))
               for m in ("model-a", "model-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for m in ("model-a", "model-b"):
        code, body = results[m]
        assert code == 200, (m, code, body)
        assert body["model"] == m, (m, body["model"])
        text = body["choices"][0]["message"]["content"]
        assert text == f"ran:{m}", (m, text)
    assert sorted(t[0] for t in stub.turns) == ["model-a", "model-b"], stub.turns

    # Unknown id -> 404 before any turn runs.
    resp = agentry.app.test_client().post("/v1/chat/completions", json={
        "model": "bogus", "messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 404, resp.status_code
    assert resp.get_json()["error"]["code"] == "model_not_found"
    assert len(stub.turns) == 2, stub.turns

    # Omitted model -> launcher default; effort rides through per-turn.
    resp = agentry.app.test_client().post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "x"}],
        "reasoning_effort": "high"})
    assert resp.status_code == 200, resp.status_code
    assert resp.get_json()["model"] == "stub-default"
    assert stub.turns[-1] == ("stub-default", "high"), stub.turns[-1]

    print("PASS: per-request model/effort isolation")


if __name__ == "__main__":
    main()
