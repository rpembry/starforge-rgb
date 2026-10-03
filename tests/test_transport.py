import errno
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from starforge_cues.core import Coordinator, FakeSink
from starforge_cues.transport import LocalServer, submit

FIXTURE = (Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_bytes()


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
            path.unlink()

    def test_reject_insecure_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp).chmod(0o755)
            with self.assertRaises(RuntimeError):
                LocalServer(Path(temp) / "cue.sock")


if __name__ == "__main__":
    unittest.main()
