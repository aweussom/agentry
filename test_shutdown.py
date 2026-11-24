"""The atexit shutdown must survive a second Ctrl-C during backend close()."""
import contextlib
import io
import unittest

import agentry


class InterruptedClose:
    closed = False

    def close(self):
        self.closed = True
        raise KeyboardInterrupt


class ShutdownTest(unittest.TestCase):
    def tearDown(self):
        agentry._backend = None

    def test_second_ctrl_c_skips_cleanup_quietly(self):
        agentry._backend = b = InterruptedClose()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            agentry._shutdown_backend()   # must not raise
        self.assertTrue(b.closed)
        self.assertIn("backend shutdown skipped", out.getvalue())

    def test_no_backend(self):
        agentry._backend = None
        agentry._shutdown_backend()


if __name__ == "__main__":
    unittest.main()
