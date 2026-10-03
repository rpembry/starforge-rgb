import errno
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest

from starforge_cues.core import Coordinator, FakeSink
from starforge_cues.transport import LocalServer, submit

FIXTURE = (Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_bytes()


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "POSIX Unix sockets required")
class TransportTests(unittest.TestCase):
    def test_local_socket_permissions_and_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "private"
            path = parent / "cue.sock"
            core = Coordinator({"rgb": FakeSink()}, clock=lambda: 1790985660.0)
            try:
                host = LocalServer(path, core)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            with host:
                worker = threading.Thread(target=host.serve_forever, daemon=True)
                worker.start()
                try:
                    self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(submit(path, FIXTURE)["result"], "accepted")
                    self.assertEqual(submit(path, FIXTURE)["reason"], "duplicate")
                finally:
                    host.shutdown()
                    worker.join()
            self.assertFalse(path.exists())
            with LocalServer(path, core):
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_partial_client_does_not_block_other_clients(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "private"
            path = parent / "cue.sock"
            core = Coordinator({"rgb": FakeSink()}, clock=lambda: 1790985660.0)
            try:
                host = LocalServer(path, core)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            errors = io.StringIO()
            with host, redirect_stderr(errors):
                worker = threading.Thread(target=host.serve_forever, daemon=True)
                worker.start()
                slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                slow.connect(str(path))
                slow.sendall(b"{")
                try:
                    start = time.monotonic()
                    self.assertEqual(submit(path, FIXTURE)["result"], "accepted")
                    self.assertLess(time.monotonic() - start, 1.5)
                finally:
                    slow.close()
                    host.shutdown()
                    worker.join()
            self.assertNotIn("Traceback", errors.getvalue())

    def test_close_preserves_replacement_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "cue.sock"
            try:
                host = LocalServer(path)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            path.unlink()
            path.write_text("replacement")
            host.close()
            self.assertEqual(path.read_text(), "replacement")

    def test_host_timer_expires_without_next_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "cue.sock"
            clock = [1790985660.0]
            rgb = FakeSink()
            core = Coordinator({"rgb": rgb}, clock=lambda: clock[0])
            try:
                host = LocalServer(path, core)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            with host:
                worker = threading.Thread(target=host.serve_forever, daemon=True)
                worker.start()
                try:
                    self.assertEqual(submit(path, FIXTURE)["result"], "accepted")
                    self.assertIsNotNone(rgb.state["rgb"])
                    clock[0] += 241
                    deadline = time.monotonic() + 2
                    while rgb.state["rgb"] is not None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertIsNone(rgb.state["rgb"])
                finally:
                    host.shutdown()
                    worker.join()

    def test_reject_insecure_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp).chmod(0o755)
            with self.assertRaises(RuntimeError):
                LocalServer(Path(temp) / "cue.sock")


if __name__ == "__main__":
    unittest.main()
