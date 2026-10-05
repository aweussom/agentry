"""Offline lifecycle regression tests for persistent Claude; no CLI or login."""
import io
import json
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from backends import BackendError, ClaudeCodeBackend


class OutputPipe:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get(timeout=5)
        if line is None:
            raise StopIteration
        return line

    def close(self):
        self.closed = True
        self.lines.put(None)


class InputPipe(io.StringIO):
    def __init__(self, process):
        super().__init__()
        self.process = process

    def flush(self):
        data = self.getvalue()
        self.seek(0)
        self.truncate()
        for line in data.splitlines():
            self.process.receive(json.loads(line)["message"]["content"])


class Process:
    def __init__(self, handler, pid):
        self.pid = pid
        self.handler = handler
        self.inputs = []
        self.stdout = OutputPipe()
        self.stderr = OutputPipe()
        self.stdin = InputPipe(self)
        self.returncode = None
        self.waited = False
        self.word = None

    def receive(self, text):
        self.inputs.append(text)
        self.handler(self, text)

    def emit(self, message):
        self.stdout.lines.put(json.dumps(message) + "\n")

    def result(self, subtype="success", **kw):
        self.emit({"type": "result", "subtype": subtype, **kw})

    def delta(self, text):
        self.emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"text": text}}})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.close()
        self.stderr.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def normal(process, text):
    if text == "/clear":
        process.word = None
        process.emit({"type": "conversation_reset", "new_conversation_id": "new"})
        process.result()
    else:
        if text.startswith("remember "):
            process.word = text.split(" ", 1)[1]
        process.delta(process.word or "NONE")
        process.result()


class ClaudeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handler = normal
        self.processes = []
        self.commands = []

        def spawn(cmd, **kw):
            self.commands.append(cmd)
            process = Process(self.handler, len(self.processes) + 1)
            self.processes.append(process)
            return process

        self.popen = patch("backends.subprocess.Popen", side_effect=spawn)
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.backend = ClaudeCodeBackend(cwd=self.tmp.name, model="default-model")
        self.backend.RESET_TIMEOUT = 0.02
        self.addCleanup(self.backend.close)
        self.backend.new_session()

    def answer(self, text="recall", **kw):
        return "".join(self.backend.prompt(text, timeout=0.05, **kw))

    def test_reuses_process_but_not_conversation(self):
        self.assertEqual(self.answer("remember SECRET"), "SECRET")
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].inputs,
                         ["remember SECRET", "/clear", "recall", "/clear", "recall"])
        self.assertFalse(self.backend.session_fresh)

    def test_new_http_session_does_not_skip_reset(self):
        self.answer("remember SECRET")
        self.backend.new_session()
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(len(self.processes), 1)

    def test_concurrent_tasks_reset_only_after_active_turn_finishes(self):
        def delayed(p, text):
            if text == "remember SECRET":
                p.word = "SECRET"
                p.delta("SECRET")
            else:
                normal(p, text)
        self.processes[0].handler = delayed
        first = self.backend.prompt("remember SECRET")
        self.assertEqual(next(first), "SECRET")
        waiting = threading.Event()
        answers = []

        def second():
            waiting.set()
            answers.append(self.answer())

        thread = threading.Thread(target=second)
        thread.start()
        self.assertTrue(waiting.wait(2))
        self.assertEqual(self.processes[0].inputs, ["remember SECRET"])
        self.processes[0].result()
        self.assertEqual(list(first), [])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(answers, ["NONE"])
        self.assertEqual(self.processes[0].inputs, ["remember SECRET", "/clear", "recall"])

    def test_idle_worker_exit_respawns_before_next_request(self):
        self.answer()
        self.processes[0].terminate()
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(len(self.processes), 2)
        self.assertTrue(self.processes[0].waited)

    def test_model_override_and_omission_restart_with_correct_defaults(self):
        self.answer(model="other")
        self.answer()
        self.assertEqual([c[c.index("--model") + 1] for c in self.commands],
                         ["default-model", "other", "default-model"])
        self.assertTrue(all(p.waited for p in self.processes[:-1]))

    def test_missing_reset_frame_falls_back_without_sending_task_to_dirty_worker(self):
        def unsupported(p, text):
            p.result() if text == "/clear" else normal(p, text)
        self.processes[0].handler = unsupported
        self.answer("remember SECRET")
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(self.processes[0].inputs, ["remember SECRET", "/clear"])
        self.assertFalse(self.backend._reuse)
        self.answer()
        self.assertEqual(len(self.processes), 3)

    def test_reset_frame_without_terminal_result_falls_back(self):
        def incomplete(p, text):
            if text == "/clear":
                p.emit({"type": "conversation_reset"})
            else:
                normal(p, text)
        self.processes[0].handler = incomplete
        self.answer()
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(len(self.processes), 2)

    def test_failed_reset_falls_back(self):
        def failed(p, text):
            if text == "/clear":
                p.emit({"type": "conversation_reset"})
                p.result("error", result="reset failed")
            else:
                normal(p, text)
        self.processes[0].handler = failed
        self.answer()
        self.assertEqual(self.answer(), "NONE")
        self.assertEqual(len(self.processes), 2)

    def test_eof_surfaces_error_then_next_task_recovers(self):
        self.processes[0].handler = lambda p, text: p.terminate()
        self.assertIn("[claude error]", self.answer())
        self.assertTrue(self.processes[0].waited)
        self.assertEqual(self.answer(), "NONE")

    def test_partial_output_then_error_is_visible_and_worker_retired(self):
        def failure(p, text):
            p.delta("partial")
            p.result("error", result="failed")
        self.processes[0].handler = failure
        answer = self.answer()
        self.assertIn("partial", answer)
        self.assertIn("[claude error] failed", answer)
        self.assertIsNone(self.backend._proc)

    def test_timeout_reaps_worker(self):
        self.processes[0].handler = lambda p, text: None
        self.assertIn("timed out", self.answer())
        self.assertTrue(self.processes[0].waited)

    def test_generator_close_retires_worker_and_ignores_its_late_output(self):
        self.processes[0].handler = lambda p, text: p.delta("first")
        stream = self.backend.prompt("x")
        self.assertEqual(next(stream), "first")
        old_events = self.backend._events
        stream.close()
        self.assertTrue(self.processes[0].waited)
        old_events.put({"type": "result", "subtype": "success"})
        self.assertEqual(self.answer(), "NONE")

    def test_cancel_idle_does_not_kill_warm_worker(self):
        self.answer()
        self.assertFalse(self.backend.cancel())
        self.assertIsNone(self.processes[0].poll())

    def test_cancel_wakes_active_request_and_allows_recovery(self):
        sent = threading.Event()
        self.processes[0].handler = lambda p, text: sent.set()
        output = []
        thread = threading.Thread(target=lambda: output.extend(
            self.backend.prompt("x", timeout=10)))
        thread.start()
        self.assertTrue(sent.wait(2))
        self.assertTrue(self.backend.cancel())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("cancelled", "".join(output))
        self.assertEqual(self.answer(), "NONE")

    def test_cancel_during_reset_does_not_respawn_or_send_next_task(self):
        self.answer()
        sent = threading.Event()
        self.processes[0].handler = lambda p, text: sent.set()
        output = []
        thread = threading.Thread(target=lambda: output.extend(self.backend.prompt("next")))
        thread.start()
        self.assertTrue(sent.wait(2))
        self.backend.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.processes), 1)
        self.assertNotIn("next", self.processes[0].inputs)

    def test_close_while_generator_is_suspended_does_not_deadlock(self):
        self.processes[0].handler = lambda p, text: p.delta("first")
        stream = self.backend.prompt("x")
        next(stream)
        self.backend.close()
        stream.close()
        self.assertFalse(self.backend.is_alive())
        with self.assertRaises(BackendError):
            self.backend.new_session()

    def test_no_duplicate_text_and_rate_limit_events_preserved(self):
        def events(p, text):
            p.emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
            p.delta("hello")
            p.emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hello"}]}})
            p.result()
        self.processes[0].handler = events
        self.assertEqual(self.answer(), "hello")
        self.assertEqual(self.backend._rate_limit, {"status": "allowed"})

    def test_whole_message_fallback(self):
        def events(p, text):
            p.emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hello"}]}})
            p.result()
        self.processes[0].handler = events
        self.assertEqual(self.answer(), "hello")

    def test_flags_keep_subscription_auth_and_disable_tools(self):
        command = self.commands[0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--bare", command)


if __name__ == "__main__":
    unittest.main()
