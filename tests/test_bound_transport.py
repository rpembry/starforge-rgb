"""Synthetic peer and scope checks for the opt-in bound listener."""

import errno
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from starforge_cues.bound_transport import BoundLocalServer
from starforge_cues.core import Coordinator, FakeSink
from starforge_cues.ingress_contract import IngressBinding, parse_frame_json
from starforge_cues.contract import ContractError


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = 1790985660.0


def binding():
    return IngressBinding("synthetic.binding", frozenset({"synthetic.build"}),
                          frozenset({"started", "succeeded", "unknown"}),
                          "info", "known", True, True, False)


def frame(**changes):
    return json.dumps({"proto": 1, "op": "publish", "event": {**FIXTURE, **changes}}).encode()


def exchange(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(path))
        conn.sendall(raw + b"\n")
        return json.loads(conn.recv(8192))


class UnknownSink(FakeSink):
    def dispatch(self, channel, plan):
        self.calls.append((channel, dict(plan)))
        return "unknown"


class EnvelopeTests(unittest.TestCase):
    def test_duplicate_keys_and_oversize_rejected(self):
        with self.assertRaises(ContractError):
            parse_frame_json(b'{"proto":1,"proto":1,"op":"hello"}')
        with self.assertRaises(ContractError):
            parse_frame_json(b"{" + b" " * 8192)


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"),
                     "Linux peer credentials required")
class BoundTransportTests(unittest.TestCase):
    def test_kernel_peer_binding_blocks_scope_and_preserves_independent_outcomes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "bound.sock"
            sinks = {"text": FakeSink(), "rgb": FakeSink(fail=frozenset({"rgb"})),
                     "audio": UnknownSink()}
            core = Coordinator(sinks, clock=lambda: NOW)
            try:
                host = BoundLocalServer(path, core, binding(), expected_uid=os.getuid())
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            with host:
                worker = threading.Thread(target=host.serve_forever, daemon=True)
                worker.start()
                try:
                    for changes in ({"source_id": "another.source"},
                                    {"severity": "critical"},
                                    {"status": "cancelled"}):
                        self.assertEqual(exchange(path, frame(**changes))["reason"],
                                         "receiver_scope")
                    self.assertEqual(len(core._seen), 0)
                    self.assertEqual(exchange(path, b'{"proto":1,"op":"hello"}')["reason"],
                                     "unsupported_operation")
                    self.assertEqual(exchange(path, b'{"proto":1,"proto":1,"op":"hello"}')["reason"],
                                     "invalid_event")
                    with patch.object(BoundLocalServer, "peer_uid", return_value=os.getuid() + 1):
                        self.assertEqual(exchange(path, frame())["reason"], "unauthorized_peer")
                    self.assertEqual(len(core._seen), 0)
                    accepted = exchange(path, frame())
                    self.assertEqual(accepted["result"], "accepted")
                    self.assertEqual(accepted["channels"],
                                     {"text": "accepted", "rgb": "failed", "audio": "unknown"})
                    self.assertEqual(exchange(path, frame())["reason"], "duplicate")
                finally:
                    host.shutdown()
                    worker.join()
            self.assertFalse(path.exists())

    def test_retraction_and_multi_source_bindings_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "bound.sock"
            with self.assertRaises(ValueError):
                BoundLocalServer(path, Coordinator(),
                                 IngressBinding("wide", frozenset({"one", "two"}),
                                                frozenset({"started"}), "info", "known",
                                                False, False, False), expected_uid=os.getuid())
            self.assertFalse(path.exists())
