"""Fake-only registered host path, session and quota checks."""

import errno
import json
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starforge_cues.contract import CueEvent
from starforge_cues.core import Coordinator, FakeSink, SourceCapabilities
from starforge_cues.ingress_contract import IngressBinding
from starforge_cues.registered_host import RegisteredHost, RuntimePathError, _validate_runtime_root


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
        root = Path("/synthetic/private")
        def info(owner, kind=stat.S_IFDIR, mode=0o700):
            return SimpleNamespace(st_uid=owner, st_mode=kind | mode)
        path_info = {Path("/synthetic"): info(0, mode=0o755), root: info(1000)}
        walk = lambda path: path_info[path]
        self.assertEqual(_validate_runtime_root(root, 1000, walk), root)
        for path, replacement in ((root, info(1000, mode=0o755)),
                                  (root, info(1000, stat.S_IFLNK)),
                                  (Path("/synthetic"), info(1000, mode=0o777)),
                                  (Path("/synthetic"), info(65534, mode=0o1777))):
            with self.subTest(path=path, mode=replacement.st_mode):
                before = path_info[path]
                path_info[path] = replacement
                with self.assertRaises(RuntimePathError):
                    _validate_runtime_root(root, 1000, walk)
                path_info[path] = before
        path_info[Path("/synthetic")] = info(0, mode=0o1777)
        self.assertEqual(_validate_runtime_root(root, 1000, walk), root)


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"),
                     "Linux peer credentials required")
class RegisteredHostTests(unittest.TestCase):
    def test_restart_epoch_replay_and_single_source_rate_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Ancestor ownership is tested above with deterministic stat data.
            # The real temp directory remains mode 0700 for actual socket checks.
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
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
        # The epoch is not an identity proof or durable replay ledger: a
        # same-UID caller can rewrap a still-fresh old event.
        self.assertEqual(exchange(path, publish(epoch))["result"], "accepted")
        for index in range(9):
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
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                with self.assertRaises(ValueError):
                    RegisteredHost(root, binding(), Coordinator())
                with self.assertRaises(ValueError):
                    RegisteredHost(root, binding(), coordinator("other.source"))
                used = coordinator()
                used.handle(CueEvent.from_mapping(FIXTURE))
                used.clock = lambda: NOW + 8000
                used.monotonic_clock = lambda: NOW + 8000
                used.tick()  # Replay and lease tables can empty after expiry.
                self.assertFalse(used._seen)
                self.assertFalse(used._leases)
                self.assertFalse(used._retired_subjects)
                self.assertFalse(used._rate)
                self.assertTrue(used._ever_handled)
                with self.assertRaises(ValueError):
                    RegisteredHost(root, binding(), used)
            self.assertFalse((root / "synthetic.binding.sock").exists())
