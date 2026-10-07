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
from threading import Event, Thread
from unittest.mock import patch

try:
    import pwd
except ImportError:
    pwd = None

from starforge_cues.core import Coordinator, SourceCapabilities
from starforge_cues.printer_ingress import PrinterIngressSettings, printer_binding
from starforge_cues.printer_mqtt import P1ReportNormalizer
from starforge_cues.printer_text_host import (PrinterForegroundSession, PrinterTextSink,
                                              text_observation_ready, with_printer_card)
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
    def test_observation_requires_loaded_visible_text_policy(self):
        snapshot = {"quiet": False}
        self.assertTrue(text_observation_ready("loaded", snapshot,
                                               unlocked=True, banners_enabled=True))
        for status, quiet, unlocked, banners in (
                ("missing", False, True, True), ("invalid", False, True, True),
                ("loaded", True, True, True), ("loaded", False, False, True),
                ("loaded", False, True, False)):
            with self.subTest(status=status, quiet=quiet, unlocked=unlocked, banners=banners):
                self.assertFalse(text_observation_ready(status, {"quiet": quiet},
                                                        unlocked=unlocked,
                                                        banners_enabled=banners))

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
        self.assertIsNotNone(sink.pinned_row())
        self.assertEqual(with_printer_card(visible, sink, frozenset())["rows"][0]["text"],
                         "Print finished.")
        sink.dismiss()
        self.assertEqual(with_printer_card(visible, sink, frozenset())["rows"], [])


@unittest.skipUnless(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED") and
                     pwd is not None,
                     "Linux peer credentials required")
class ForegroundSessionTests(unittest.TestCase):
    def test_stalled_server_readiness_times_out_then_late_worker_exits_on_retry_close(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = PrinterIngressSettings(pwd.getpwuid(os.geteuid()).pw_name, root)
            normalizer = P1ReportNormalizer(SERIAL, clock=lambda: NOW)
            binding = printer_binding(normalizer)
            source = next(iter(binding.source_ids))
            core = Coordinator({"text": PrinterTextSink()}, clock=lambda: NOW,
                               sources={source: SourceCapabilities(binding.statuses, True, True)})
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                try:
                    session = PrinterForegroundSession(settings, normalizer, core, FakeClient())
                except PermissionError as exc:
                    if exc.errno == errno.EPERM:
                        self.skipTest("sandbox denies Unix socket bind")
                    raise
            entered, release = Event(), Event()
            original_loop = session._serve_loop

            def stalled_loop():
                entered.set()
                release.wait(timeout=3)
                original_loop()  # Sees the stop request before it can accept.

            session._serve_loop = stalled_loop
            with patch("starforge_cues.printer_text_host.STARTUP_READY_S", 0.05), \
                 patch("starforge_cues.printer_text_host.STOP_JOIN_S", 0.05):
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    session.start()
            self.assertTrue(entered.is_set())
            self.assertEqual(session._close_result["server_stopped"], False)
            self.assertFalse((root / "printer.local.sock").exists())
            release.set()
            session.server_thread.join(timeout=1)
            self.assertFalse(session.server_thread.is_alive())
            self.assertEqual(session.close(),
                             {"listener_closed": True, "server_stopped": True,
                              "collector_stopped": True})
            self.assertIsNone(session.collector_thread)

    def test_each_worker_start_failure_cleans_up_without_joining_unstarted_thread(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                settings = PrinterIngressSettings(pwd.getpwuid(os.geteuid()).pw_name, root)
                normalizer = P1ReportNormalizer(SERIAL, clock=lambda: NOW)
                binding = printer_binding(normalizer)
                source = next(iter(binding.source_ids))
                core = Coordinator({"text": PrinterTextSink()}, clock=lambda: NOW,
                                   sources={source: SourceCapabilities(binding.statuses, True, True)})
                with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                    try:
                        session = PrinterForegroundSession(settings, normalizer, core, FakeClient())
                    except PermissionError as exc:
                        if exc.errno == errno.EPERM:
                            self.skipTest("sandbox denies Unix socket bind")
                        raise
                original_start = Thread.start
                calls = [0]

                def selected_start(thread):
                    calls[0] += 1
                    if calls[0] == fail_at:
                        raise RuntimeError("synthetic worker start failure")
                    return original_start(thread)

                with patch("starforge_cues.printer_text_host.Thread.start", selected_start):
                    with self.assertRaisesRegex(RuntimeError, "startup failed"):
                        session.start()
                self.assertEqual(calls[0], fail_at)
                self.assertEqual(session.close(),
                                 {"listener_closed": True, "server_stopped": True,
                                  "collector_stopped": True})
                self.assertFalse((root / "printer.local.sock").exists())
                if session.server_thread is not None:
                    self.assertFalse(session.server_thread.is_alive())

    def test_unstopped_collector_is_reported_as_failed_cleanup(self):
        class BlockedThread:
            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = PrinterIngressSettings(pwd.getpwuid(os.geteuid()).pw_name, root)
            normalizer = P1ReportNormalizer(SERIAL, clock=lambda: NOW)
            binding = printer_binding(normalizer)
            source = next(iter(binding.source_ids))
            core = Coordinator({"text": PrinterTextSink()}, clock=lambda: NOW,
                               sources={source: SourceCapabilities(binding.statuses, True, True)})
            with patch("starforge_cues.registered_host.validate_runtime_root", return_value=root):
                try:
                    session = PrinterForegroundSession(settings, normalizer, core, FakeClient())
                except PermissionError as exc:
                    if exc.errno == errno.EPERM:
                        self.skipTest("sandbox denies Unix socket bind")
                    raise
            session.collector_thread = BlockedThread()
            self.assertEqual(session.close(),
                             {"listener_closed": True, "server_stopped": True,
                              "collector_stopped": False})
            self.assertEqual(session.error, "foreground_stop_unconfirmed")
            self.assertEqual(session.close()["collector_stopped"], False)

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
            self.assertEqual(result, {"listener_closed": True, "server_stopped": True,
                                      "collector_stopped": True})
            self.assertFalse((root / "printer.local.sock").exists())
