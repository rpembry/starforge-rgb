"""Synthetic printer collector through bound UDS into fake outputs."""

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

try:
    import pwd
except ImportError:
    pwd = None

from starforge_cues.core import Coordinator, FakeSink, SourceCapabilities
from starforge_cues.printer_ingress import (
    BoundPrinterPublisher, IngressSettingsError, PrinterIngressSettings,
    allowed_uid, parse_printer_ingress_settings, printer_binding,
    read_private_printer_ingress_settings, registered_printer_host,
)
from starforge_cues.printer_mqtt import P1ReportNormalizer, collect_once


NOW = 1791158400.0
SERIAL = "01P0000000000000"


class FakeClient:
    tls_verified = True
    peer_pin_verified = True
    auth_accepted = True
    subscription_accepted = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def reports(self):
        for state in ("RUNNING", "FINISH", "FINISH"):
            yield json.dumps({"print": {"gcode_state": state,
                                         "task_id": "synthetic-task"}}).encode()


def settings(root):
    return PrinterIngressSettings(pwd.getpwuid(os.geteuid()).pw_name, root)


@unittest.skipUnless(pwd is not None and os.name == "posix",
                     "POSIX private account settings required")
class SettingsTests(unittest.TestCase):
    def test_private_data_and_existing_account_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "printer-ingress.json"
            raw = json.dumps({"version": 1, "allowed_account": settings(root).allowed_account,
                              "runtime_root": str(root)}).encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            self.assertEqual(read_private_printer_ingress_settings(path).runtime_root, root)
            self.assertEqual(allowed_uid(settings(root)), os.geteuid())
            path.chmod(0o644)
            with self.assertRaises(IngressSettingsError):
                read_private_printer_ingress_settings(path)
            path.chmod(0o600)
            root.chmod(0o755)
            with self.assertRaises(IngressSettingsError):
                read_private_printer_ingress_settings(path)
            root.chmod(0o700)
            with self.assertRaises(IngressSettingsError):
                allowed_uid(PrinterIngressSettings("nonexistent_starforge_account", root))
            with patch("starforge_cues.printer_ingress.pwd.getpwnam") as lookup:
                lookup.return_value.pw_uid = os.geteuid() + 1
                with self.assertRaises(IngressSettingsError):
                    allowed_uid(settings(root))
        for raw in (b'{"version":1,"version":1}', b"{}", b" " * 1025,
                    b'{"version":1,"allowed_account":"root","runtime_root":"../bad"}'):
            with self.subTest(raw=raw[:40]), self.assertRaises(IngressSettingsError):
                parse_printer_ingress_settings(raw)


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"),
                     "Linux peer credentials required")
class PrinterBoundTests(unittest.TestCase):
    def test_collector_completion_reaches_only_fake_outputs_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normalizer = P1ReportNormalizer(SERIAL, clock=lambda: NOW,
                                            monotonic=lambda: NOW)
            binding = printer_binding(normalizer)
            source = next(iter(binding.source_ids))
            sinks = {channel: FakeSink() for channel in ("text", "rgb", "audio")}
            core = Coordinator(sinks, clock=lambda: NOW,
                               sources={source: SourceCapabilities(binding.statuses, True, True)})
            # The path validator has deterministic ownership tests elsewhere;
            # this test exercises actual UDS/peer credentials in rootless QA too.
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                try:
                    host = registered_printer_host(settings(root), normalizer, core)
                except PermissionError as exc:
                    if exc.errno == errno.EPERM:
                        self.skipTest("sandbox denies Unix socket bind")
                    raise
            receipts = []
            with host.listener:
                worker = threading.Thread(target=host.listener.serve_forever, daemon=True)
                worker.start()
                try:
                    publisher = BoundPrinterPublisher(Path(host.listener.server_address))
                    collect_once(FakeClient(), normalizer,
                                 lambda event: receipts.append(publisher.publish(event)),
                                 monotonic=lambda: NOW)
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0]["result"], "accepted")
                    self.assertEqual(receipts[0]["channels"],
                                     {channel: "accepted" for channel in sinks})
                    self.assertEqual(len(core._seen), 1)
                    self.assertEqual(len(sinks["text"].calls) > 0, True)
                    self.assertEqual(core._active_key[0], source)
                finally:
                    host.listener.shutdown()
                    worker.join()
            self.assertFalse(Path(host.listener.server_address).exists())
