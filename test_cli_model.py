"""Offline regression checks for CLI model selection."""

import io
import sys
import threading
import unittest
from urllib.request import urlopen
from unittest.mock import Mock, patch

import agentry


class CliModelTest(unittest.TestCase):
    def test_mixed_case_model_uses_canonical_id(self):
        backend = Mock()
        backend.list_models.return_value = [{"id": "gpt-6-sol"}]
        backend.auth_login = None
        backend.session_id = "test-session"

        with (patch.object(sys, "argv", ["agentry.py", "--backend", "codex",
                                         "--model", "gpt-6-Sol",
                                         "--reasoning-effort", "Medium"]),
              patch.object(agentry, "_get_backend", return_value=backend),
              patch.object(agentry, "start_keepalive"),
              patch.object(agentry, "BACKEND_KIND", "copilot"),
              patch.object(agentry, "BACKEND_MODEL", None),
              patch.object(agentry, "REASONING_EFFORT", None),
              patch.object(agentry, "LOG_DIR") as log_dir,
              patch.object(agentry, "ExclusiveThreadedWSGIServer") as server_type):
            server = server_type.return_value.__enter__.return_value
            server.port = 8765
            agentry.main()
            self.assertEqual(agentry.BACKEND_MODEL, "gpt-6-sol")
            self.assertEqual(agentry.REASONING_EFFORT, "medium")

        backend.new_session.assert_called_once_with()
        server.serve_forever.assert_called_once_with()
        log_dir.mkdir.assert_called_once_with(exist_ok=True)

    def test_occupied_port_exits_before_backend_start(self):
        with agentry.ExclusiveThreadedWSGIServer("0.0.0.0", 0, agentry.app) as first:
            with (patch.object(sys, "argv", ["agentry.py", "--port", str(first.port)]),
                  patch.object(agentry, "_get_backend") as get_backend,
                  patch.object(agentry, "start_keepalive") as keepalive,
                  patch.object(agentry, "LOG_DIR"),
                  patch.object(sys, "stderr", new_callable=io.StringIO) as errors):
                with self.assertRaises(SystemExit) as exit_info:
                    agentry.main()

            self.assertEqual(exit_info.exception.code, 1)
            get_backend.assert_not_called()
            keepalive.assert_not_called()
            self.assertIn(f"Port {first.port} is in use", errors.getvalue())

    def test_bound_server_serves_http(self):
        with agentry.ExclusiveThreadedWSGIServer("127.0.0.1", 0, agentry.app) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.port}/health", timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
