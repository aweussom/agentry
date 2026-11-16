"""Offline regression tests for the agy ACP backend and its lockdown hook.

A fake `agy acp` speaks just enough ACP: initialize, authenticate,
session/new with agy's '<base>-<level>' model ids, set_model, session/prompt
streaming agent_message_chunk / agent_thought_chunk / tool_call frames,
cancel. The image subagent is faked by writing a JPEG into brain/<session>/
after the turn has ended, which is what agy 1.5.9 does.

The hook tests run agy_hook.py exactly as agy does: a fresh interpreter,
the call as JSON on stdin, a decision as JSON on stdout.
"""
import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backends import AgyACPBackend, BackendError

HERE = Path(__file__).parent

MODELS = [{"modelId": f"{base}-{lv}", "name": f"{name} ({lv.title()})"}
          for base, name, levels in (
              ("gemini-3.8-flash", "Gemini 3.8 Flash", ("high", "medium", "low")),
              ("gemini-4-hydrogen", "Gemini 4 Hydrogen", ("high", "xhigh", "medium", "low")),
              ("gemini-3.1-pro", "Gemini 3.1 Pro", ("high", "low")))
          for lv in levels]

JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x04\x00\x04\x00" + b"\x00" * 64


class Pipe:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        line = self.lines.get()
        return "" if line is None else line

    def close(self):
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


class FakeAgy:
    def __init__(self, brain_root):
        self.brain_root = brain_root
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.stdin = Stdin(self)
        self.returncode = None
        self.received = []
        self.sessions = 0
        self.turn = None
        self.script = None
        self.hang_prompt = False
        self.ask_permission = False
        self.auth_error = None

    def emit(self, msg):
        self.stdout.lines.put(json.dumps(msg) + "\n")

    def respond(self, msg_id, result):
        self.emit({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def update(self, sid, upd):
        self.emit({"jsonrpc": "2.0", "method": "session/update",
                   "params": {"sessionId": sid, "update": upd}})

    def text(self, sid, t):
        self.update(sid, {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": t}})

    def thought(self, sid, t):
        self.update(sid, {"sessionUpdate": "agent_thought_chunk",
                          "content": {"type": "text", "text": t}})

    def finish(self, msg_id, stop="end_turn"):
        self.respond(msg_id, {"stopReason": stop, "usage": {
            "inputTokens": 100, "outputTokens": 5, "cachedReadTokens": 10,
            "thoughtTokens": 3, "totalTokens": 118}})
        self.turn = None

    def sent(self, method):
        return [m for m in self.received if m.get("method") == method]

    def receive(self, msg):
        self.received.append(msg)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            self.respond(mid, {"protocolVersion": 1,
                               "agentCapabilities": {"promptCapabilities": {"image": True}},
                               "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.9"}})
        elif method == "authenticate":
            if self.auth_error:
                self.emit({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32000, "message": self.auth_error}})
            else:
                self.respond(mid, {})
        elif method == "session/new":
            self.sessions += 1
            self.respond(mid, {"sessionId": f"s{self.sessions}",
                               "models": {"availableModels": MODELS,
                                          "currentModelId": "gemini-3.1-pro-low"}})
        elif method in ("session/set_model", "session/close"):
            self.respond(mid, {})
        elif method == "session/prompt":
            sid = params["sessionId"]
            self.turn = (mid, sid)
            if self.hang_prompt:
                return
            if self.ask_permission:
                self.emit({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                           "params": {"sessionId": sid}})
            (self.script or self.default_script)(self, mid, sid, params["prompt"])
        elif method == "session/cancel":
            if self.turn:
                self.finish(self.turn[0], stop="cancelled")

    @staticmethod
    def default_script(proc, mid, sid, blocks):
        proc.thought(sid, "thinking")
        proc.text(sid, "echo:")
        proc.text(sid, blocks[-1]["text"])
        proc.finish(mid)

    def image_script(self, delay=0.3, write=True):
        """Dispatch the image subagent, end the turn, write the file later."""
        def script(proc, mid, sid, blocks):
            proc.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                              "title": "Run invoke_subagent", "kind": "other",
                              "rawInput": {"Prompt": "a red cube", "Role": "Image Generator",
                                           "TypeName": "image-generator"}})
            proc.text(sid, "dispatched")
            proc.finish(mid)
            if not write:
                return

            def later():
                time.sleep(delay)
                d = Path(proc.brain_root) / sid
                d.mkdir(parents=True, exist_ok=True)
                (d / "red_cube_1.jpg").write_bytes(JPEG)
            threading.Thread(target=later, daemon=True).start()
        return script

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


class AgyBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.procs = []
        self.commands = []
        self.env = None
        self.auth_error = None

        def spawn(cmd, **kw):
            self.commands.append(cmd)
            self.env = kw.get("env")
            brain = self.home / cmd[cmd.index("--gemini-dir") + 1] / "antigravity-cli" / "brain"
            p = FakeAgy(brain)
            p.auth_error = self.auth_error
            self.procs.append(p)
            return p

        for p in (patch("backends.subprocess.Popen", side_effect=spawn),
                  patch.object(Path, "home", return_value=self.home),
                  patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})):
            p.start()
            self.addCleanup(p.stop)

    def make(self, **kw):
        b = AgyACPBackend(cwd=os.path.join(self.tmp.name, "cwd"), **kw)
        self.addCleanup(b.close)
        return b

    @property
    def proc(self):
        return self.procs[-1]

    def collect(self, gen):
        return list(gen)

    # -- setup and auth --

    def test_api_key_auth_passes_key_and_writes_hook_in_own_dir(self):
        b = self.make()
        self.assertIn("--gemini-dir", self.commands[0])
        self.assertIn(".gemini-agentry-api-key", self.commands[0])
        self.assertEqual(self.env["GEMINI_API_KEY"], "test-key")
        self.assertEqual(self.proc.sent("authenticate")[0]["params"]["methodId"], "gemini-api-key")
        hooks = json.loads((self.home / ".gemini-agentry-api-key" / "config" / "hooks.json")
                           .read_text(encoding="utf-8"))
        handler = hooks["agentry-lockdown"]["PreToolUse"][0]
        self.assertEqual(handler["matcher"], "*")
        self.assertIn("agy_hook.py", handler["hooks"][0]["command"])
        self.assertFalse((self.home / ".gemini").exists(), "must not touch the user's ~/.gemini")
        self.assertEqual(b.auth, "api-key")

    def test_oauth_strips_api_keys_and_uses_separate_dir(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "g"}):
            self.make(auth="oauth")
        self.assertNotIn("GEMINI_API_KEY", self.env)
        self.assertNotIn("GOOGLE_API_KEY", self.env)
        self.assertIn(".gemini-agentry-oauth", self.commands[0])
        self.assertEqual(self.proc.sent("authenticate")[0]["params"]["methodId"], "oauth-personal")

    def test_oauth_failure_names_the_login_command(self):
        self.auth_error = "Authentication required"
        with self.assertRaises(BackendError) as cm:
            self.make(auth="oauth")
        self.assertIn("--gemini-dir .gemini-agentry-oauth --login", str(cm.exception))

    def test_missing_api_key_fails_loud(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY")
            with patch.object(AgyACPBackend, "KEYS_INI", Path(self.tmp.name) / "nope.ini"):
                with self.assertRaises(BackendError) as cm:
                    self.make()
        self.assertIn("GEMINI_API_KEY", str(cm.exception))
        self.assertEqual(self.procs, [], "agy must not start without a key")

    def test_api_key_from_keys_ini(self):
        ini = Path(self.tmp.name) / "keys.ini"
        ini.write_text("[gemini]\nGEMINI_API_KEY = from-ini\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY")
            with patch.object(AgyACPBackend, "KEYS_INI", ini):
                self.make()
        self.assertEqual(self.env["GEMINI_API_KEY"], "from-ini")

    def test_unknown_auth_rejected(self):
        with self.assertRaises(BackendError):
            self.make(auth="adc")

    # -- selection --

    def test_session_uses_agy_default_and_effort(self):
        b = self.make(reasoning_effort="high")
        b.new_session()
        self.assertEqual(b.current_model(), "gemini-3.1-pro")
        set_model = self.proc.sent("session/set_model")
        self.assertEqual(set_model[-1]["params"]["modelId"], "gemini-3.1-pro-high")
        meta = self.proc.sent("session/new")[0]["params"]["_meta"]
        self.assertIn("Your tools are disabled",
                      meta["antigravity"]["systemPrompt"]["append"])

    def test_per_request_model_and_nearest_lower_effort(self):
        b = self.make()
        b.new_session()
        self.collect(b.prompt("hi", model="gemini-3.8-flash", effort="xhigh"))
        self.assertEqual(self.proc.sent("session/set_model")[-1]["params"]["modelId"],
                         "gemini-3.8-flash-high")
        self.collect(b.prompt("hi", model="gemini-4-hydrogen", effort="ultra"))
        self.assertEqual(self.proc.sent("session/set_model")[-1]["params"]["modelId"],
                         "gemini-4-hydrogen-xhigh")
        before = len(self.proc.sent("session/set_model"))
        self.collect(b.prompt("hi", model="gemini-4-hydrogen", effort="ultra"))
        self.assertEqual(len(self.proc.sent("session/set_model")), before,
                         "same selection must not be re-sent")

    def test_unknown_model_is_a_visible_error(self):
        b = self.make()
        b.new_session()
        out = self.collect(b.prompt("hi", model="gpt-5"))
        self.assertTrue(any("unknown agy model" in o for o in out if isinstance(o, str)))
        self.assertEqual(self.proc.sent("session/prompt"), [])

    def test_list_models_groups_levels(self):
        b = self.make()
        b.new_session()
        models = {m["id"]: m for m in b.list_models()}
        self.assertEqual(set(models), {"gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.1-pro"})
        self.assertEqual(models["gemini-3.1-pro"]["name"], "Gemini 3.1 Pro")
        self.assertTrue(models["gemini-3.1-pro"]["isDefault"])
        efforts = [e["reasoningEffort"] for e in models["gemini-4-hydrogen"]["supportedReasoningEfforts"]]
        self.assertIn("xhigh", efforts)

    # -- turns --

    def test_prompt_streams_text_and_reasoning_and_counts_tokens(self):
        b = self.make()
        b.new_session()
        out = self.collect(b.prompt("hello"))
        self.assertEqual(out, [("reasoning", "thinking"), "echo:", "hello"])
        self.assertFalse(b.session_fresh)
        self.assertIn("in=100", b.quota_status())

    def test_images_ride_the_prompt_as_blocks(self):
        b = self.make()
        b.new_session()
        self.collect(b.prompt("what is this", images=[("image/png", "QUJD")]))
        blocks = self.proc.sent("session/prompt")[-1]["params"]["prompt"]
        self.assertEqual(blocks[0], {"type": "image", "mimeType": "image/png", "data": "QUJD"})
        self.assertEqual(blocks[1]["type"], "text")
        # The image subagent needs a path inside the hook's allowed root.
        ref = self.home / ".gemini-agentry-api-key" / "antigravity-cli" / "brain" / b.session_id / "refs" / "ref-1.png"
        self.assertEqual(ref.read_bytes(), b"ABC")
        self.assertIn(str(ref).replace("\\", "/"), blocks[1]["text"])
        self.assertTrue(blocks[1]["text"].endswith("what is this"))

    def test_async_image_subagent_file_is_collected(self):
        b = self.make()
        b.new_session()
        self.proc.script = self.proc.image_script(delay=0.3)
        out = self.collect(b.prompt("draw a red cube"))
        imgs = [o for o in out if isinstance(o, tuple) and o[0] == "image"]
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0][1], "image/jpeg")
        self.assertEqual(base64.b64decode(imgs[0][2]), JPEG)
        self.assertEqual(imgs[0][3], "a red cube")

    def test_image_wait_times_out_with_visible_error(self):
        b = self.make()
        b.new_session()
        b.IMAGE_WAIT_S = 0.5
        self.proc.script = self.proc.image_script(write=False)
        out = self.collect(b.prompt("draw"))
        self.assertTrue(any("produced no file" in o for o in out if isinstance(o, str)))

    def test_image_turn_uses_scratch_session_and_closes_it(self):
        b = self.make()
        b.new_session()
        chat_sid = b.session_id
        self.proc.script = self.proc.image_script(delay=0.2)
        out = self.collect(b.image_turn("a red cube", images=[("image/jpeg", "QUJD")]))
        self.assertTrue(any(isinstance(o, tuple) and o[0] == "image" for o in out))
        prompt = self.proc.sent("session/prompt")[-1]["params"]
        self.assertNotEqual(prompt["sessionId"], chat_sid)
        self.assertEqual(prompt["prompt"][0]["type"], "image")
        self.assertIn("/refs/ref-1.jpg", prompt["prompt"][1]["text"])
        closed = [m["params"]["sessionId"] for m in self.proc.sent("session/close")]
        self.assertIn(prompt["sessionId"], closed)
        self.assertEqual(b.session_id, chat_sid)
        refs = self.home / ".gemini-agentry-api-key" / "antigravity-cli" / "brain" / prompt["sessionId"] / "refs"
        self.assertFalse(refs.exists(), "our reference copies go with the scratch session")
        generated = refs.parent / "red_cube_1.jpg"
        self.assertTrue(generated.exists(), "agy's generated image stays")

    def test_cancel_sends_session_cancel(self):
        b = self.make()
        b.new_session()
        self.proc.hang_prompt = True
        gen = b.prompt("long")
        t = threading.Thread(target=lambda: self.collect(gen))
        t.start()
        deadline = time.time() + 5
        while not self.proc.sent("session/prompt") and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(b.cancel())
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(self.proc.sent("session/cancel")), 1)

    def test_abandoned_turn_is_cancelled(self):
        b = self.make()
        b.new_session()
        self.proc.script = lambda proc, mid, sid, blocks: proc.text(sid, "partial")
        gen = b.prompt("x")
        self.assertEqual(next(gen), "partial")
        gen.close()
        self.assertEqual(len(self.proc.sent("session/cancel")), 1)

    def test_permission_request_is_cancelled(self):
        b = self.make()
        b.new_session()
        self.proc.ask_permission = True
        self.collect(b.prompt("x"))
        replies = [m for m in self.proc.received if m.get("id") == 9001]
        self.assertEqual(replies[0]["result"], {"outcome": {"outcome": "cancelled"}})


class AgyHookTests(unittest.TestCase):
    """agy_hook.py as agy runs it: a fresh interpreter, JSON in, JSON out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brain = Path(self.tmp.name) / "brain"
        self.brain.mkdir()
        self.deny_log = Path(self.tmp.name) / "denied.jsonl"

    def run_hook(self, payload):
        r = subprocess.run([sys.executable, "-I", str(HERE / "agy_hook.py"),
                            str(self.brain), str(self.deny_log)],
                           input=payload, capture_output=True, text=True, timeout=30)
        return r

    def decide(self, name, args):
        r = self.run_hook(json.dumps({"toolCall": {"name": name, "args": args},
                                      "conversationId": "c1"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["decision"]

    def test_read_and_command_tools_denied_and_logged(self):
        for name in ("read_file", "run_command", "search_web", "read_url",
                     "run_workflow", "write_file", "manage_subagents"):
            self.assertEqual(self.decide(name, {"Path": "x"}), "deny", name)
        logged = [json.loads(l)["tool"] for l in self.deny_log.read_text().splitlines()]
        self.assertIn("run_command", logged)

    def test_only_image_subagent_allowed(self):
        self.assertEqual(self.decide("invoke_subagent", {"TypeName": "image-generator"}), "allow")
        self.assertEqual(self.decide("invoke_subagent", {"TypeName": "general"}), "deny")

    def test_generate_image_refs_must_be_inside_brain(self):
        inside = str(self.brain / "s1" / ".user_uploaded" / "a.jpg")
        outside = str(Path(self.tmp.name) / "secret.jpg")
        self.assertEqual(self.decide("generate_image", {"Prompt": "x"}), "allow")
        self.assertEqual(self.decide("generate_image", {"ImagePaths": [inside]}), "allow")
        self.assertEqual(self.decide("generate_image", {"ImagePaths": [inside, outside]}), "deny")

    def test_read_file_only_inside_brain(self):
        inside = str(self.brain / "s1" / "refs" / "ref-1.jpg")
        self.assertEqual(self.decide("read_file", {"Path": inside}), "allow")
        self.assertEqual(self.decide("read_file", {"Path": str(self.brain / ".." / "x.txt")}), "deny")
        self.assertEqual(self.decide("read_file", {}), "deny")

    def test_send_message_allowed(self):
        self.assertEqual(self.decide("send_message", {"Message": "done"}), "allow")

    def test_garbage_input_exits_nonzero(self):
        # agy treats a failing hook as a deny (probed); this must not print allow.
        r = self.run_hook("not json")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("allow", r.stdout)


if __name__ == "__main__":
    unittest.main()
