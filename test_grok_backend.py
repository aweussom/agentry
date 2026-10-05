"""Offline regression tests for the Grok ACP backend; no CLI or login.

A fake `grok agent stdio` speaks just enough ACP: initialize with a model
list, session/new, set_model / set_config_option, session/prompt streaming
agent_message_chunk / agent_thought_chunk / image_gen tool frames, cancel.
"""
import base64
import json
import os
import re
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backends import BackendError, GrokACPBackend


MODELS = [
    {"modelId": "grok-4.7", "name": "Grok 4.7", "_meta": {"reasoningEfforts": [
        {"id": e} for e in ("xhigh", "high", "medium", "low")]}},
    {"modelId": "grok-4.7-build-fast", "name": "Grok 4.7 Fast", "_meta": {"reasoningEfforts": [
        {"id": e} for e in ("xhigh", "high", "medium", "low")]}},
    {"modelId": "grok-4.5", "name": "Grok 4.5", "_meta": {"reasoningEfforts": [
        {"id": e} for e in ("high", "medium", "low")]}},
]


class Pipe:
    """Blocking line source for the backend's readline loops."""

    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def readline(self):
        line = self.lines.get()
        return "" if line is None else line

    def close(self):
        self.closed = True
        self.lines.put(None)


class Stdin:
    def __init__(self, proc):
        self.proc = proc
        self.buf = ""
        self.closed = False

    def write(self, s):
        self.buf += s

    def flush(self):
        data, self.buf = self.buf, ""
        for line in data.splitlines():
            if line.strip():
                self.proc.receive(json.loads(line))

    def close(self):
        self.closed = True
        self.proc.on_eof()


class FakeGrok:
    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.stdin = Stdin(self)
        self.returncode = None
        self.received = []          # every JSON-RPC message from the backend
        self.sessions = 0
        self.turn = None            # (msg id, sessionId) of the running prompt
        self.script = None          # callable(proc, msg_id, sid, text) for prompts
        self.hang_prompt = False
        self.ask_permission = False

    # -- helpers --
    def emit(self, msg):
        self.stdout.lines.put(json.dumps(msg) + "\n")

    def respond(self, msg_id, result):
        self.emit({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def update(self, sid, upd, method="session/update"):
        self.emit({"jsonrpc": "2.0", "method": method,
                   "params": {"sessionId": sid, "update": upd}})

    def text(self, sid, t):
        self.update(sid, {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": t}})

    def thought(self, sid, t):
        self.update(sid, {"sessionUpdate": "agent_thought_chunk",
                          "content": {"type": "text", "text": t}})

    def finish(self, msg_id, sid, stop="end_turn", ticks=112438000):
        self.update(sid, {"sessionUpdate": "turn_completed", "stop_reason": stop,
                          "usage": {"inputTokens": 100, "outputTokens": 5,
                                    "cachedReadTokens": 10, "reasoningTokens": 3,
                                    "costUsdTicks": ticks}},
                    method="_x.ai/session_notification")
        self.respond(msg_id, {"stopReason": stop})
        self.turn = None

    def sent(self, method):
        return [m for m in self.received if m.get("method") == method]

    # -- process protocol --
    def receive(self, msg):
        self.received.append(msg)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            self.respond(mid, {"protocolVersion": 1,
                               "agentCapabilities": {"promptCapabilities": {"image": False}},
                               "_meta": {"agentVersion": "1.0.46",
                                         "modelState": {"currentModelId": "grok-4.7",
                                                        "availableModels": MODELS}}})
        elif method == "session/new":
            self.sessions += 1
            self.emit({"jsonrpc": "2.0", "method": "_x.ai/models/update", "params": {}})
            self.respond(mid, {"sessionId": f"s{self.sessions}",
                               "models": {"currentModelId": "grok-4.7"}})
        elif method in ("session/set_model", "session/set_config_option", "session/close"):
            self.respond(mid, {})
        elif method == "session/prompt":
            sid = params["sessionId"]
            self.turn = (mid, sid)
            if self.hang_prompt:
                return
            if self.ask_permission:
                self.emit({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                           "params": {"sessionId": sid}})
            text = params["prompt"][0]["text"]
            (self.script or self.default_script)(self, mid, sid, text)
        elif method == "session/cancel":
            if self.turn:
                mid2, sid = self.turn
                self.finish(mid2, sid, stop="cancelled", ticks=0)
        elif "id" in msg and "error" in msg:
            pass    # our refusal of a permission request

    @staticmethod
    def default_script(proc, mid, sid, text):
        proc.thought(sid, "thinking")
        proc.text(sid, "echo:")
        proc.text(sid, text)
        proc.finish(mid, sid)

    def on_eof(self):
        self.returncode = 0
        self.stdout.close()
        self.stderr.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.on_eof()

    def kill(self):
        self.on_eof()


class GrokBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.procs = []
        self.commands = []
        self.env = None

        def spawn(cmd, **kw):
            self.commands.append(cmd)
            self.env = kw.get("env")
            p = FakeGrok(self.tmp.name)
            self.procs.append(p)
            return p

        self.popen = patch("backends.subprocess.Popen", side_effect=spawn)
        self.popen.start()
        self.addCleanup(self.popen.stop)

    def make(self, **kw):
        b = GrokACPBackend(cwd=self.tmp.name, **kw)
        self.addCleanup(b.close)
        return b

    @property
    def proc(self):
        return self.procs[-1]

    def collect(self, gen):
        text, reasoning, images = [], [], []
        for d in gen:
            if isinstance(d, tuple) and d[0] == "reasoning":
                reasoning.append(d[1])
            elif isinstance(d, tuple) and d[0] == "image":
                images.append(d)
            else:
                text.append(d)
        return "".join(text), "".join(reasoning), images

    # -- tests --

    def test_spawn_uses_profile_and_strips_api_key(self):
        with patch.dict(os.environ, {"XAI_API_KEY": "xai-secret"}):
            self.make()
        cmd = self.commands[0]
        self.assertEqual(cmd[1:3], ["agent", "--agent-profile"])
        self.assertEqual(cmd[-1], "stdio")
        self.assertTrue(cmd[3].endswith("grok-agent-profile.md"))
        self.assertNotIn("XAI_API_KEY", self.env)

    def test_new_session_applies_defaults_and_prompt_streams(self):
        b = self.make(reasoning_effort="ultra")
        sid = b.new_session()
        self.assertEqual(sid, "s1")
        self.assertTrue(b.session_fresh)
        # ultra -> xhigh on grok-4.7; model already current so no set_model
        self.assertEqual(self.proc.sent("session/set_model"), [])
        eff = self.proc.sent("session/set_config_option")
        self.assertEqual(eff[-1]["params"], {"sessionId": "s1", "configId": "reasoning_effort",
                                             "value": "xhigh"})
        text, reasoning, images = self.collect(b.prompt("hi", timeout=2))
        self.assertEqual(text, "echo:hi")
        self.assertEqual(reasoning, "thinking")
        self.assertEqual(images, [])
        self.assertFalse(b.session_fresh)
        self.assertIn("this run ~$0.011", b.quota_status())

    def test_per_request_model_and_effort_switch_inside_turn(self):
        b = self.make()
        b.new_session()
        self.proc.received.clear()
        self.collect(b.prompt("a", timeout=2, model="grok-4.5", effort="xhigh"))
        order = [m["method"] for m in self.proc.received]
        self.assertEqual(order, ["session/set_model", "session/set_config_option",
                                 "session/prompt"])
        # xhigh is not offered on grok-4.5 -> nearest lower rung
        self.assertEqual(self.proc.sent("session/set_config_option")[0]["params"]["value"],
                         "high")
        self.proc.received.clear()
        # Omitted selection falls back to launcher defaults (grok-4.7 @ high)
        self.collect(b.prompt("b", timeout=2))
        self.assertEqual(self.proc.sent("session/set_model")[0]["params"]["modelId"],
                         "grok-4.7")
        self.assertEqual(self.proc.sent("session/set_config_option")[0]["params"]["value"],
                         "high")
        self.proc.received.clear()
        # Same selection again: nothing re-sent
        self.collect(b.prompt("c", timeout=2))
        self.assertEqual([m["method"] for m in self.proc.received], ["session/prompt"])

    def test_list_models_hides_fast_variant(self):
        b = self.make()
        ids = [m["id"] for m in b.list_models()]
        self.assertEqual(ids, ["grok-4.7", "grok-4.5"])
        default = [m for m in b.list_models() if m["isDefault"]]
        self.assertEqual(default[0]["id"], "grok-4.7")
        self.assertEqual([e["reasoningEffort"] for e in default[0]["supportedReasoningEfforts"]],
                         ["xhigh", "high", "medium", "low"])

    def test_attachments_go_to_disk_and_are_named_in_the_prompt(self):
        b = self.make()
        b.new_session()
        png = base64.b64encode(b"\x89PNGdata").decode()
        text, _, _ = self.collect(b.prompt("what is this", timeout=2, images=[("image/png", png)]))
        self.assertNotIn("dropped", text)
        sent = self.proc.sent("session/prompt")[0]["params"]["prompt"]
        self.assertEqual([p["type"] for p in sent], ["text"])
        body = sent[0]["text"]
        self.assertTrue(body.startswith("[The user attached 1 image file(s)"))
        self.assertTrue(body.endswith("what is this"))
        path = re.search(r"(\S+/refs/s1/ref-1\.png)", body).group(1)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(open(path, "rb").read(), b"\x89PNGdata")
        self.assertIn("read_file", body)
        # Kept for the session: a later turn in the same chat can still name it
        self.collect(b.prompt("and now?", timeout=2))
        self.assertTrue(os.path.isfile(path))
        # A second attachment in the same session gets the next number
        self.collect(b.prompt("more", timeout=2, images=[("image/jpeg", png)]))
        self.assertTrue(os.path.isfile(os.path.join(b.refs_root, "s1", "ref-2.jpg")))
        # New chat: old session closed, its attachments gone
        b.new_session()
        self.assertEqual(self.proc.sent("session/close")[-1]["params"], {"sessionId": "s1"})
        self.assertFalse(os.path.exists(os.path.join(b.refs_root, "s1")))

    def test_session_new_registers_the_tool_gate_hook(self):
        b = self.make()
        b.new_session()
        meta = self.proc.sent("session/new")[0]["params"]["_meta"]
        groups = meta["x.ai/hooks"]["PreToolUse"]
        self.assertEqual(groups[0]["matcher"], "*")
        self.assertEqual(groups[0]["hookCallbackIds"], [b.HOOK_CALLBACK_ID])

    def test_hook_decisions(self):
        b = self.make()
        inside = os.path.join(b.refs_root, "s1", "ref-1.jpg")
        os.makedirs(os.path.dirname(inside), exist_ok=True)
        open(inside, "wb").write(b"x")
        grok_img = os.path.join(str(Path.home()), ".grok", "sessions", "x", "images", "1.jpg")
        outside = os.path.join(self.tmp.name, "secret.jpg")
        traversal = os.path.join(b.refs_root, "s1", "..", "..", "secret.jpg")

        def verdict(tool, **inp):
            return b._hook_decision({"toolName": tool, "toolInput": inp})

        self.assertEqual(verdict("image_gen", prompt="x"), {})
        self.assertEqual(verdict("read_file", target_file=inside), {})
        self.assertEqual(verdict("read_file", target_file=inside.replace("\\", "/")), {})
        self.assertEqual(verdict("read_file", target_file=grok_img), {})
        self.assertEqual(verdict("read_file", target_file=outside)["decision"], "deny")
        self.assertEqual(verdict("read_file", target_file=traversal)["decision"], "deny")
        self.assertEqual(verdict("read_file")["decision"], "deny")
        self.assertEqual(verdict("image_edit", image=[inside, grok_img]), {})
        self.assertEqual(verdict("image_edit", image=[inside, outside])["decision"], "deny")
        self.assertEqual(verdict("image_edit", image=[])["decision"], "deny")
        self.assertEqual(verdict("run_terminal_command", command="ls")["decision"], "deny")
        self.assertEqual(verdict(None)["decision"], "deny")

    def test_hook_run_request_is_answered_on_the_wire(self):
        b = self.make()
        b.new_session()
        outside = os.path.join(self.tmp.name, "win.ini")

        def script(proc, mid, sid, text):
            proc.emit({"jsonrpc": "2.0", "id": 7001, "method": "_x.ai/hooks/run",
                       "params": {"hookCallbackId": b.HOOK_CALLBACK_ID, "toolName": "read_file",
                                  "toolInput": {"target_file": outside}}})
            proc.emit({"jsonrpc": "2.0", "id": 7002, "method": "_x.ai/hooks/run",
                       "params": {"hookCallbackId": b.HOOK_CALLBACK_ID, "toolName": "image_gen",
                                  "toolInput": {"prompt": "x"}}})
            proc.text(sid, "ok")
            proc.finish(mid, sid)

        self.proc.script = script
        self.collect(b.prompt("x", timeout=2))
        replies = {m["id"]: m for m in self.proc.received if m.get("id") in (7001, 7002)}
        self.assertEqual(replies[7001]["result"]["decision"], "deny")
        self.assertEqual(replies[7002]["result"], {})

    def test_image_gen_tool_result_becomes_image_tuple(self):
        jpg = os.path.join(self.tmp.name, "1.jpg")
        with open(jpg, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fakejpeg")

        def script(proc, mid, sid, text):
            proc.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                              "title": "image_gen", "rawInput": {"prompt": "a red square"},
                              "_meta": {"x.ai/tool": {"name": "image_gen"}}})
            proc.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                              "status": "in_progress", "content": []})
            proc.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                              "status": "completed",
                              "content": [{"type": "content", "content": {
                                  "type": "text", "text": json.dumps({"path": jpg})}}]})
            proc.text(sid, "Done.")
            proc.finish(mid, sid)

        b = self.make()
        b.new_session()
        self.proc.script = script
        text, _, images = self.collect(b.prompt("draw", timeout=2))
        self.assertEqual(text, "Done.")
        self.assertEqual(len(images), 1)
        kind, mime, b64, prompt = images[0]
        self.assertEqual((kind, mime, prompt), ("image", "image/jpeg", "a red square"))
        self.assertEqual(base64.b64decode(b64), b"\xff\xd8\xff\xe0fakejpeg")

    def test_failed_image_tool_is_visible(self):
        def script(proc, mid, sid, text):
            proc.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                              "title": "image_gen", "rawInput": {"prompt": "x"}})
            proc.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                              "status": "failed",
                              "content": [{"type": "content", "content": {
                                  "type": "text", "text": "usage limit"}}]})
            proc.finish(mid, sid)

        b = self.make()
        b.new_session()
        self.proc.script = script
        text, _, images = self.collect(b.prompt("draw", timeout=2))
        self.assertIn("[grok image error] usage limit", text)
        self.assertEqual(images, [])

    def test_permission_request_is_refused(self):
        b = self.make()
        b.new_session()
        self.proc.ask_permission = True
        self.collect(b.prompt("rm -rf", timeout=2))
        refusals = [m for m in self.proc.received if m.get("id") == 9001 and "error" in m]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["error"]["code"], -32601)

    def test_cancel_ends_turn_and_session_survives(self):
        b = self.make()
        b.new_session()
        self.proc.hang_prompt = True
        gen = b.prompt("slow", timeout=5)
        got = []
        t = threading.Thread(target=lambda: got.extend(gen))
        t.start()
        for _ in range(100):
            if self.proc.turn:
                break
            time.sleep(0.01)
        self.assertFalse(b.cancel() is False)
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.proc.sent("session/cancel")[0]["params"], {"sessionId": "s1"})
        self.assertEqual(got, [])
        self.assertFalse(b.cancel())           # nothing in flight now
        self.proc.hang_prompt = False
        text, _, _ = self.collect(b.prompt("again", timeout=2))
        self.assertEqual(text, "echo:again")

    def test_timeout_cancels_and_reports(self):
        b = self.make()
        b.new_session()
        self.proc.hang_prompt = True
        text, _, _ = self.collect(b.prompt("slow", timeout=0.05))
        self.assertIn("[grok timeout after 0.05s]", text)
        self.assertEqual(len(self.proc.sent("session/cancel")), 1)

    def test_generator_close_cancels_abandoned_turn(self):
        def script(proc, mid, sid, text):
            proc.text(sid, "first")
            proc.text(sid, "second")
            # no finish: the turn is still running when the client leaves

        b = self.make()
        b.new_session()
        self.proc.script = script
        gen = b.prompt("go", timeout=2)
        self.assertEqual(next(gen), "first")
        gen.close()
        self.assertEqual(len(self.proc.sent("session/cancel")), 1)
        self.assertIsNone(b.active_turn_queue)

    def test_other_sessions_updates_are_ignored(self):
        def script(proc, mid, sid, text):
            proc.text("stale-session", "LEAK")
            proc.text(sid, "ok")
            proc.finish(mid, sid)

        b = self.make()
        b.new_session()
        self.proc.script = script
        text, _, _ = self.collect(b.prompt("x", timeout=2))
        self.assertEqual(text, "ok")

    def test_new_session_auth_error_mentions_login(self):
        b = self.make()
        orig = self.proc.receive

        def receive(msg):
            if msg.get("method") == "session/new":
                self.proc.emit({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32000, "message": "not authenticated"}})
                return
            orig(msg)

        self.proc.receive = receive
        with self.assertRaises(BackendError) as cm:
            b.new_session()
        self.assertIn("grok login", str(cm.exception))

    def test_prompt_without_session_raises(self):
        b = self.make()
        with self.assertRaises(BackendError):
            list(b.prompt("x"))

    def test_missing_profile_fails_fast(self):
        with self.assertRaises(BackendError):
            GrokACPBackend(cwd=self.tmp.name,
                           profile_path=os.path.join(self.tmp.name, "nope.md"))
        self.assertEqual(self.commands, [])


    def _image_script(self, jpg, expect_tool):
        def script(proc, mid, sid, text):
            proc.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                              "title": expect_tool, "rawInput": {"prompt": "p", "image": []},
                              "_meta": {"x.ai/tool": {"name": expect_tool}}})
            proc.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                              "status": "completed",
                              "content": [{"type": "content", "content": {
                                  "type": "text", "text": json.dumps({"path": jpg})}}]})
            proc.text(sid, "Saved.")
            proc.finish(mid, sid)
        return script

    def test_image_turn_generation_uses_scratch_session(self):
        jpg = os.path.join(self.tmp.name, "out.jpg")
        with open(jpg, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0gen")
        b = self.make()
        b.new_session()
        self.proc.script = self._image_script(jpg, "image_gen")
        self.proc.received.clear()
        text, _, images = self.collect(b.image_turn("a red square. The image MUST be square"))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][1], "image/jpeg")
        methods = [m["method"] for m in self.proc.received]
        self.assertEqual(methods[0], "session/new")
        self.assertIn("session/prompt", methods)
        self.assertEqual(methods[-1], "session/close")
        sent = self.proc.sent("session/prompt")[0]["params"]
        self.assertEqual(sent["sessionId"], "s2")          # not the chat session
        self.assertIn("image generation tool (image_gen)", sent["prompt"][0]["text"])
        self.assertTrue(sent["prompt"][0]["text"].endswith("The image MUST be square"))
        self.assertEqual(b.session_id, "s1")
        self.assertTrue(b.session_fresh)                   # chat session untouched

    def test_image_turn_edit_writes_references_then_removes_them(self):
        jpg = os.path.join(self.tmp.name, "out.jpg")
        with open(jpg, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0edit")
        b = self.make()
        b.new_session()
        seen = {}

        def script(proc, mid, sid, text):
            paths = re.findall(r"[A-Za-z]:/[^ ,]+ref-\d\.(?:jpg|png)", text)
            seen["paths"] = paths
            seen["exist"] = [os.path.isfile(p) for p in paths]
            seen["bytes"] = [open(p, "rb").read() for p in paths]
            self._image_script(jpg, "image_edit")(proc, mid, sid, text)

        self.proc.script = script
        refs = [("image/png", base64.b64encode(b"\x89PNGone").decode()),
                ("image/jpeg", base64.b64encode(b"\xff\xd8two").decode())]
        text, _, images = self.collect(b.image_turn("add a hat", images=refs))
        self.assertEqual(len(images), 1)
        self.assertEqual(len(seen["paths"]), 2)
        self.assertTrue(seen["paths"][0].endswith("ref-1.png"))
        self.assertTrue(seen["paths"][1].endswith("ref-2.jpg"))
        self.assertEqual(seen["exist"], [True, True])
        self.assertEqual(seen["bytes"], [b"\x89PNGone", b"\xff\xd8two"])
        self.assertTrue(all(not os.path.exists(p) for p in seen["paths"]))
        sent = self.proc.sent("session/prompt")[0]["params"]["prompt"][0]["text"]
        self.assertIn("image editing tool (image_edit)", sent)
        self.assertIn("2 reference image file(s)", sent)
        # the "image(s) dropped" note must NOT appear: refs went via disk
        self.assertNotIn("dropped", text)
        self.assertEqual([m["method"] for m in self.proc.received][-1], "session/close")

    def test_image_turn_cleans_up_when_turn_fails(self):
        b = self.make()
        b.new_session()

        def script(proc, mid, sid, text):
            proc.emit({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32000, "message": "boom"}})
            proc.turn = None

        self.proc.script = script
        refs = [("image/png", base64.b64encode(b"x").decode())]
        text, _, images = self.collect(b.image_turn("x", images=refs))
        self.assertIn("[grok error] boom", text)
        self.assertEqual(images, [])
        self.assertEqual([m["method"] for m in self.proc.received][-1], "session/close")
        self.assertFalse(os.listdir(os.path.join(self.tmp.name, "refs")))


if __name__ == "__main__":
    unittest.main()
