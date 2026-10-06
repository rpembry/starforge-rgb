"""Fake-only registered host path, session and quota checks."""

import errno
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

from starforge_cues.core import Coordinator, FakeSink, SourceCapabilities
from starforge_cues.ingress_contract import IngressBinding
from starforge_cues.registered_host import RegisteredHost, RuntimePathError, validate_runtime_root


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = 1790985660.0


def binding():
    return IngressBinding("synthetic.binding", frozenset({"synthetic.build"}),
                          frozenset({"started", "succeeded", "unknown"}),
                          "info", "known", True, True, False)


def coordinator(source="synthetic.build"):
    return Coordinator({"rgb": FakeSink()}, clock=lambda: NOW,
                       sources={source: SourceCapabilities(
                           frozenset({"started", "succeeded", "unknown"}), True, True)})


def exchange(path, value):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(path))
        conn.sendall(json.dumps(value).encode() + b"\n")
        return json.loads(conn.recv(8192))


def publish(epoch, **changes):
    return {"proto": 1, "op": "publish", "session_epoch": epoch,
            "event": {**FIXTURE, **changes}}


class RuntimeRootTests(unittest.TestCase):
    def test_private_root_and_ancestors_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(validate_runtime_root(root), root)
            root.chmod(0o755)
            with self.assertRaises(RuntimePathError):
                validate_runtime_root(root)
            root.chmod(0o700)
            link = root / "alias"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(RuntimePathError):
                validate_runtime_root(link)
            outer = root / "outer"
            outer.mkdir(mode=0o700)
            private = outer / "private"
            private.mkdir(mode=0o700)
            outer.chmod(0o777)
            with self.assertRaises(RuntimePathError):
                validate_runtime_root(private)
            self.assertFalse((private / "synthetic.binding.sock").exists())


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"),
                     "Linux peer credentials required")
class RegisteredHostTests(unittest.TestCase):
    def test_restart_epoch_replay_and_single_source_rate_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            try:
                old = RegisteredHost(root, binding(), coordinator())
                self._run(old, self._first_session)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            fresh = RegisteredHost(root, binding(), coordinator())
            self.assertNotEqual(old.session_epoch, fresh.session_epoch)
            self._run(fresh, lambda path: self._new_session(path, old.session_epoch, fresh))
            self.assertFalse((root / "synthetic.binding.sock").exists())

    def _run(self, host, check):
        with host.listener:
            worker = threading.Thread(target=host.listener.serve_forever, daemon=True)
            worker.start()
            try:
                check(Path(host.listener.server_address))
            finally:
                host.listener.shutdown()
                worker.join()

    def _first_session(self, path):
        hello = exchange(path, {"proto": 1, "op": "hello"})
        self.assertEqual(hello["result"], "ready")
        epoch = hello["session_epoch"]
        self.assertEqual(exchange(path, publish(epoch))["result"], "accepted")
        self.assertEqual(exchange(path, publish(epoch))["reason"], "duplicate")
        self.assertEqual(exchange(path, publish(epoch, source_id="other.source"))["reason"],
                         "receiver_scope")
        self.assertEqual(exchange(path, {"proto": 1, "op": "retract"})["reason"],
                         "invalid_event")

    def _new_session(self, path, old_epoch, host):
        self.assertEqual(exchange(path, publish(old_epoch))["reason"], "invalid_event")
        self.assertEqual(len(host.coordinator._seen), 0)
        epoch = exchange(path, {"proto": 1, "op": "hello"})["session_epoch"]
        self.assertEqual(epoch, host.session_epoch)
        for index in range(10):
            result = exchange(path, publish(epoch, event_id=f"new-{index}",
                                            idempotency_key=f"new-{index}",
                                            subject_id=None))
            self.assertEqual(result["result"], "accepted")
        blocked = exchange(path, publish(epoch, event_id="new-eleven",
                                         idempotency_key="new-eleven", subject_id=None))
        self.assertEqual(blocked["reason"], "rate_limit")
        self.assertEqual(set(host.coordinator.sources), {"synthetic.build"})

    def test_unregistered_or_nonfresh_coordinator_cannot_open_listener(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                RegisteredHost(root, binding(), Coordinator())
            with self.assertRaises(ValueError):
                RegisteredHost(root, binding(), coordinator("other.source"))
            self.assertFalse((root / "synthetic.binding.sock").exists())
