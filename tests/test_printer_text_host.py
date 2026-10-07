"""Foreground printer text path with fake reports and no live sinks."""

from datetime import datetime, timezone
from contextlib import redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import pwd
except ImportError:
    pwd = None

from starforge_cues.core import Coordinator, SourceCapabilities
from starforge_cues.printer_ingress import PrinterIngressSettings, printer_binding
from starforge_cues.printer_mqtt import P1ReportNormalizer
from starforge_cues.printer_text_host import (PrinterForegroundSession, PrinterTextSink,
                                              with_printer_card)
from starforge_cues.text_window import main as text_window_main


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).timestamp()
SERIAL = "01P0000000000000"


class FakeClient:
    stream = None
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


class PrinterCardTests(unittest.TestCase):
    def test_printer_mode_refuses_audio_before_any_gtk_or_printer_use(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            text_window_main(["--printer-text", "--config", "/unused/settings.json",
                              "--ingress-config", "/unused/ingress.json",
                              "--printer-host", "10.0.0.1", "--printer-serial", SERIAL,
                              "--printer-ca-file", "/unused/ca.pem",
                              "--printer-peer-sha256", "0" * 64,
                              "--manual-audio-sink", "forbidden"])
        self.assertEqual(caught.exception.code, 2)

    def test_hold_dismiss_and_quiet_or_lock_hiding(self):
        sink = PrinterTextSink()
        plan = {"operation": "cue", "cue_id": "printer.completed",
                "source_id": "printer.synthetic", "status": "succeeded",
                "severity": "info", "confidence": "known", "text": "Print finished.",
                "occurred_at": "2026-10-07T12:00:00+00:00"}
        self.assertEqual(sink.dispatch("text", plan), "accepted")
        visible = {"hidden": False, "rows": [], "history": [], "overflow": 0, "truncated": 0}
        self.assertEqual(with_printer_card(visible, sink, frozenset())["rows"][0]["text"],
                         "Print finished.")
        self.assertEqual(with_printer_card(visible, sink,
                                           frozenset({"printer.synthetic"}))["rows"], [])
        self.assertEqual(with_printer_card({**visible, "hidden": True}, sink,
                                           frozenset())["rows"], [])
        self.assertIsNone(sink.pinned_row())
        self.assertEqual(with_printer_card(visible, sink, frozenset())["rows"], [])


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED") and
                     pwd is not None,
                     "Linux peer credentials required")
class ForegroundSessionTests(unittest.TestCase):
    def test_fake_completion_reaches_text_and_session_reaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = pwd.getpwuid(os.geteuid()).pw_name
            settings = PrinterIngressSettings(account, root)
            normalizer = P1ReportNormalizer(SERIAL, clock=lambda: NOW,
                                            monotonic=lambda: NOW)
            binding = printer_binding(normalizer)
            source = next(iter(binding.source_ids))
            sink = PrinterTextSink()
            core = Coordinator({"text": sink}, clock=lambda: NOW,
                               sources={source: SourceCapabilities(binding.statuses, True, True)})
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                try:
                    session = PrinterForegroundSession(settings, normalizer, core, FakeClient())
                except PermissionError as exc:
                    if exc.errno == errno.EPERM:
                        self.skipTest("sandbox denies Unix socket bind")
                    raise
            session.start()
            session.collector_thread.join(timeout=3)
            self.assertFalse(session.collector_thread.is_alive())
            self.assertEqual(session.error, None)
            self.assertEqual(len(session.receipts), 1)
            self.assertEqual(session.receipts[0]["channels"]["text"], "accepted")
            self.assertEqual(sink.pinned_row()["text"], "Print finished.")
            result = session.close()
            self.assertEqual(result, {"listener_closed": True, "collector_stopped": True})
            self.assertFalse((root / "printer.local.sock").exists())
