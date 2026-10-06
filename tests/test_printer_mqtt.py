"""Synthetic P1 MQTT and credential tests; no network, printer, or real secret."""

from datetime import datetime, timezone
import contextlib
import errno
import hashlib
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from starforge_cues.printer_mqtt import (
    CollectorError, P1ReportNormalizer, SubscribeOnlyP1Client, _open_tls,
    MAX_STATE_LEASE_S, _packet, _read_packet, collect_once, diagnostic_code, parse_fingerprint,
    read_private_access_code, validate_address,
)
from starforge_cues.printer_status import PrinterObservation


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()

    def tick(self, seconds):
        self.value += seconds


def report(state, task="synthetic-task"):
    return json.dumps({"print": {"gcode_state": state, "task_id": task}}).encode()


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.normalizer = P1ReportNormalizer("01P0000000000000", clock=lambda: self.clock.value,
                                             monotonic=lambda: self.clock.value)
        self.normalizer.connected()

    def test_confirmed_finish_and_repeat_are_suppressed(self):
        self.assertEqual(self.normalizer.accept(report("RUNNING")).reason, "active")
        self.assertEqual(self.normalizer.accept(report("PAUSE")).reason, "paused")
        event = self.normalizer.accept(report("FINISH")).event
        self.assertEqual((event.cue_id, event.text), ("printer.completed", "Print finished."))
        self.assertNotIn("synthetic-task", repr(event))
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "duplicate_terminal")

    def test_startup_terminal_reconnect_gap_and_unknown_do_not_complete(self):
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "unproven_terminal")
        self.normalizer.accept(report("RUNNING", "second-task"))
        self.normalizer.disconnected()
        self.normalizer.connected()
        self.assertEqual(self.normalizer.accept(report("FINISH", "second-task")).reason,
                         "unproven_terminal")
        self.normalizer.accept(report("RUNNING", "third-task"))
        self.clock.tick(61)
        self.assertEqual(self.normalizer.accept(report("FINISH", "third-task")).reason,
                         "unproven_terminal")
        self.normalizer.accept(report("RUNNING", "fourth-task"))
        self.normalizer.accept(report("OFFLINE", "fourth-task"))
        self.assertEqual(self.normalizer.accept(report("FINISH", "fourth-task")).reason,
                         "unproven_terminal")

    def test_disconnect_rejects_delayed_terminal_until_fresh_printing(self):
        self.normalizer.accept(report("RUNNING", "old-task"))
        old_epoch = self.normalizer.epoch
        self.normalizer.disconnected()
        with self.assertRaisesRegex(CollectorError, "disconnected"):
            self.normalizer.accept(report("FINISH", "old-task"))
        self.normalizer.connected()
        self.assertGreater(self.normalizer.epoch, old_epoch)
        self.assertEqual(self.normalizer.accept(report("FINISH", "old-task")).reason,
                         "unproven_terminal")
        self.normalizer.accept(report("RUNNING", "new-task"))
        self.assertEqual(self.normalizer.accept(report("FINISH", "new-task")).outcome,
                         "emitted")

    def test_partial_missing_task_and_failure_remain_unknown(self):
        self.normalizer.accept(report("RUNNING"))
        self.assertEqual(self.normalizer.accept(b'{"print":{"mc_percent":42}}').reason,
                         "no_state")
        self.assertEqual(self.normalizer.accept(report("FINISH", None)).reason,
                         "unknown")
        self.normalizer.accept(report("RUNNING"))
        self.assertEqual(self.normalizer.accept(report("FAILED")).reason, "unknown")
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "unproven_terminal")

    def test_conflicting_task_and_invalid_reports_clear_proof(self):
        self.normalizer.accept(report("RUNNING", "one"))
        self.assertEqual(self.normalizer.accept(report("FINISH", "two")).reason,
                         "unproven_terminal")
        self.assertEqual(self.normalizer.accept(report("FINISH", "one")).reason,
                         "unproven_terminal")
        self.normalizer.accept(report("RUNNING", "three"))
        with self.assertRaises(CollectorError):
            self.normalizer.accept(b'{"print":{"gcode_state":"RUNNING","gcode_state":"FINISH"}}')
        self.assertEqual(self.normalizer.accept(report("FINISH", "three")).reason,
                         "unproven_terminal")
        with self.assertRaises(CollectorError):
            self.normalizer.accept(b"x" * 16385)
        self.assertEqual(self.normalizer.accept(report("FINISH", "three")).reason,
                         "unproven_terminal")

    def test_sparse_delta_reports_preserve_only_bounded_active_proof(self):
        self.assertEqual(self.normalizer.accept(b'{"print":{"mc_percent":42}}').reason,
                         "no_state")
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "unproven_terminal")
        self.assertEqual(self.normalizer.accept(report("RUNNING")).reason, "active")
        for _ in range(43):
            self.clock.tick(2)
            self.assertEqual(self.normalizer.accept(b'{"print":{"mc_percent":42}}').reason,
                             "no_state")
        self.clock.tick(2)
        self.assertEqual(self.normalizer.accept(report("FINISH")).outcome, "emitted")

        self.normalizer.disconnected()
        self.normalizer.connected()
        self.normalizer.accept(b'{"print":{"mc_percent":42}}')
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "duplicate_terminal")

    def test_sparse_deltas_and_old_packet_preserve_newer_completion_proof(self):
        self.assertEqual(self.normalizer.accept(report("RUNNING")).reason, "active")
        older = PrinterObservation(
            self.normalizer.epoch, 0,
            datetime.fromtimestamp(self.clock.value - 30, timezone.utc),
            "printing", self.normalizer.adapter.active_job_id)
        self.assertEqual(self.normalizer.adapter.observe(older).reason, "out_of_order")
        for _ in range(43):
            self.clock.tick(2)
            self.assertEqual(self.normalizer.accept(b'{"print":{"mc_percent":42}}').reason,
                             "no_state")
        self.clock.tick(2)
        self.assertEqual(self.normalizer.accept(report("FINISH")).outcome, "emitted")

    def test_packet_gap_and_state_lease_expire_partial_delta_proof(self):
        self.normalizer.accept(report("RUNNING"))
        self.clock.tick(61)
        self.assertEqual(self.normalizer.accept(report("FINISH")).reason,
                         "unproven_terminal")

        self.normalizer.disconnected()
        self.normalizer.connected()
        self.normalizer.accept(report("RUNNING", "long-task"))
        for _ in range(MAX_STATE_LEASE_S // 60 + 1):
            self.clock.tick(60)
            self.normalizer.accept(b'{"print":{"mc_percent":42}}')
        self.assertEqual(self.normalizer.accept(report("FINISH", "long-task")).reason,
                         "unproven_terminal")

    def test_conflicting_task_in_delta_erases_active_proof(self):
        self.normalizer.accept(report("RUNNING", "first-task"))
        self.assertEqual(self.normalizer.accept(
            b'{"print":{"task_id":"other-task","mc_percent":42}}').reason,
            "unknown")
        self.assertEqual(self.normalizer.last_input_issue, "task_changed_without_state")
        self.assertEqual(self.normalizer.accept(report("FINISH", "first-task")).reason,
                         "unproven_terminal")

    def test_invalidation_survives_wall_clock_rollback(self):
        for path in ("packet_gap", "state_lease", "invalid_json", "invalid_shape",
                     "unsupported_state", "idle", "conflicting_delta",
                     "different_terminal", "reconnect"):
            with self.subTest(path=path):
                wall = Clock()
                monotonic = Clock()
                normalizer = P1ReportNormalizer(
                    "01P0000000000000", clock=lambda: wall.value,
                    monotonic=lambda: monotonic.value)
                normalizer.connected()
                normalizer.accept(report("RUNNING", "first-task"))
                wall.tick(-120)
                if path == "packet_gap":
                    monotonic.tick(61)
                    normalizer.accept(b'{"print":{"mc_percent":42}}')
                elif path == "state_lease":
                    monotonic.tick(MAX_STATE_LEASE_S + 1)
                    normalizer.last_input_at = monotonic.value - 1  # recent delta stream
                    normalizer.accept(b'{"print":{"mc_percent":42}}')
                elif path == "invalid_json":
                    with self.assertRaises(CollectorError):
                        normalizer.accept(b'{"print":')
                elif path == "invalid_shape":
                    normalizer.accept(b'{"print":[]}')
                elif path == "unsupported_state":
                    normalizer.accept(report("OFFLINE", "first-task"))
                elif path == "idle":
                    normalizer.accept(report("IDLE", "first-task"))
                elif path == "conflicting_delta":
                    normalizer.accept(b'{"print":{"task_id":"other-task"}}')
                elif path == "different_terminal":
                    normalizer.accept(report("FINISH", "other-task"))
                else:
                    normalizer.disconnected()
                    normalizer.connected()
                self.assertIsNone(normalizer.adapter.active_job_id)
                wall.tick(240)
                monotonic.tick(1)
                self.assertIsNone(normalizer.accept(report("FINISH", "first-task")).event)


class CredentialTests(unittest.TestCase):
    def test_private_file_and_rejected_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "credentials"
            directory.mkdir(mode=0o700)
            path = directory / "access-code"
            path.write_bytes(b"ABCDEFGH\n")
            path.chmod(0o600)
            self.assertEqual(read_private_access_code(path), b"ABCDEFGH")
            path.chmod(0o644)
            with self.assertRaises(CollectorError):
                read_private_access_code(path)
            path.chmod(0o600)
            link = directory / "link"
            link.symlink_to(path)
            with self.assertRaises(CollectorError):
                read_private_access_code(link)
            directory.chmod(0o755)
            with self.assertRaises(CollectorError):
                read_private_access_code(path)

    def test_address_and_pin_are_bounded(self):
        self.assertEqual(validate_address("192.168.1.2", "01P0000000000000")[0], "192.168.1.2")
        for host in ("example.org", "127.0.0.1", "8.8.8.8", "::1"):
            with self.subTest(host=host), self.assertRaises(CollectorError):
                validate_address(host, "01P0000000000000")
        with self.assertRaises(CollectorError):
            parse_fingerprint("weak")


class FakeTLS:
    def __init__(self, certificate, inbound=b""):
        self.certificate = certificate
        self.inbound = bytearray(inbound)
        self.sent = []
        self.closed = False

    def getpeercert(self, *, binary_form):
        assert binary_form
        return self.certificate

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        data = bytes(self.inbound[:size])
        del self.inbound[:size]
        return data

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def make_client(self, stream, pin, loader=lambda _: b"ABCDEFGH"):
        return SubscribeOnlyP1Client(
            "10.1.2.3", "01P0000000000000", Path("unused-ca.pem"), pin,
            Path("unused-secret"), tls_opener=lambda *_: stream,
            credential_loader=loader)

    def test_pin_before_credential_and_no_publish_packet(self):
        stream = FakeTLS(b"untrusted")
        client = self.make_client(stream, hashlib.sha256(b"trusted").hexdigest(),
                                  loader=lambda _: self.fail("credential loaded before pin"))
        with self.assertRaises(CollectorError):
            client.__enter__()
        self.assertEqual(stream.sent, [])
        self.assertTrue(stream.closed)

        topic = b"device/01P0000000000000/report"
        publish = _packet(0x30, len(topic).to_bytes(2, "big") + topic + report("RUNNING"))
        stream = FakeTLS(b"trusted", b"\x20\x02\x00\x00\x90\x03\x00\x01\x00" + publish)
        client = self.make_client(stream, hashlib.sha256(b"trusted").hexdigest())
        with client:
            self.assertEqual(next(client.reports()), report("RUNNING"))
        self.assertEqual([packet[0] for packet in stream.sent], [0x10, 0x82, 0xE0])
        self.assertTrue(stream.closed)

    def test_idle_heartbeat_and_second_timeout_end_session(self):
        class SilentTLS(FakeTLS):
            def recv(self, size):
                if not self.inbound:
                    raise socket.timeout()
                return super().recv(size)

        stream = SilentTLS(b"trusted", b"\x20\x02\x00\x00\x90\x03\x00\x01\x00")
        client = self.make_client(stream, hashlib.sha256(b"trusted").hexdigest())
        with client:
            with self.assertRaisesRegex(CollectorError, "heartbeat timed out"):
                next(client.reports())
        self.assertEqual([packet[0] for packet in stream.sent], [0x10, 0x82, 0xC0, 0xE0])

    def test_packet_bound_and_semantic_callback_seam(self):
        stream = FakeTLS(b"trusted", b"\x30\xff\xff\x7f")
        with self.assertRaises(CollectorError):
            _read_packet(stream)
        class Truncated(FakeTLS):
            def recv(self, size):
                if not self.inbound:
                    raise socket.timeout()
                return super().recv(size)
        with self.assertRaises(CollectorError):
            _read_packet(Truncated(b"trusted", b"\x30\x03\x00"))
        clock = Clock()
        normalizer = P1ReportNormalizer("01P0000000000000", clock=lambda: clock.value,
                                        monotonic=lambda: clock.value)
        events = []

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def reports(self):
                yield report("RUNNING")
                yield report("FINISH")

        collect_once(FakeClient(), normalizer, events.append)
        self.assertEqual([event.cue_id for event in events], ["printer.completed"])
        self.assertIsNone(normalizer.last_state_at)


class ProbeDiagnosticTests(unittest.TestCase):
    def test_legacy_ca_option_is_explicit_and_preserves_ca_verification(self):
        flags = ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                context = Mock()
                context.verify_flags = flags
                context.verify_mode = ssl.CERT_REQUIRED
                context.wrap_socket.return_value = Mock()
                with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                           return_value=context) as create, patch(
                               "starforge_cues.printer_mqtt.socket.create_connection"):
                    _open_tls("192.168.1.2", Path("synthetic-ca"),
                              bambu_legacy_ca=enabled)
                create.assert_called_once_with(cafile="synthetic-ca")
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertEqual(context.verify_flags,
                                 flags & ~ssl.VERIFY_X509_STRICT if enabled else flags)
                self.assertFalse(context.check_hostname)
                self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_legacy_ca_still_rejects_bad_chain_and_expiry_before_secret(self):
        for verify_code in (20, 10):  # issuer unavailable, certificate expired
            with self.subTest(verify_code=verify_code):
                failure = ssl.SSLCertVerificationError(1, "synthetic certificate failure")
                failure.verify_code = verify_code
                context = Mock()
                context.verify_flags = ssl.VERIFY_X509_STRICT
                context.wrap_socket.side_effect = failure
                raw = Mock()
                client = SubscribeOnlyP1Client(
                    "192.168.1.2", "01P0000000000000", Path("synthetic-ca"),
                    hashlib.sha256(b"expected-leaf").hexdigest(), Path("unused-secret"),
                    credential_loader=lambda _: self.fail("secret read before valid TLS"),
                    bambu_legacy_ca=True)
                with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                           return_value=context), patch(
                               "starforge_cues.printer_mqtt.socket.create_connection",
                               return_value=raw):
                    with self.assertRaises(CollectorError) as caught:
                        client.__enter__()
                self.assertEqual(diagnostic_code(caught.exception),
                                 f"tls_ca_validation_failed_x509_{verify_code}")
                raw.close.assert_called_once()

    def test_legacy_ca_still_requires_exact_leaf_pin_before_secret(self):
        stream = FakeTLS(b"different-leaf")
        def opener(_host, _ca_file, *, bambu_legacy_ca):
            self.assertIs(bambu_legacy_ca, True)
            return stream
        client = SubscribeOnlyP1Client(
            "192.168.1.2", "01P0000000000000", Path("synthetic-ca"),
            hashlib.sha256(b"expected-leaf").hexdigest(), Path("unused-secret"),
            tls_opener=opener,
            credential_loader=lambda _: self.fail("secret read before pin"),
            bambu_legacy_ca=True)
        with self.assertRaises(CollectorError) as caught:
            client.__enter__()
        self.assertEqual(diagnostic_code(caught.exception), "peer_pin_mismatch")
        self.assertEqual(stream.sent, [])
        self.assertTrue(stream.closed)

    def test_cli_legacy_ca_flag_is_explicit(self):
        from starforge_cues.cli import main
        for flagged in (False, True):
            with self.subTest(flagged=flagged):
                argv = ["printer-cert-probe", "--host", "192.168.1.2",
                        "--ca-file", "synthetic-ca"]
                if flagged:
                    argv.append("--bambu-legacy-ca")
                with patch("starforge_cues.printer_mqtt.probe_certificate",
                           return_value="0" * 64) as probe, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(argv), 0)
                probe.assert_called_once_with("192.168.1.2", Path("synthetic-ca"),
                                              bambu_legacy_ca=flagged)

    def test_ca_connect_and_tls_stages_have_distinct_redacted_codes(self):
        with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                   side_effect=FileNotFoundError("private/path/to/ca")):
            with self.assertRaises(CollectorError) as caught:
                _open_tls("192.168.1.2", Path("synthetic-ca"))
        self.assertEqual(diagnostic_code(caught.exception), "ca_file_missing")

        context = Mock()
        for failure, expected in (
                (ConnectionRefusedError(), "connection_refused"),
                (socket.timeout(), "connection_timeout"),
                (OSError(errno.ENETUNREACH, "unreachable"), "network_unreachable")):
            with self.subTest(expected=expected):
                with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                           return_value=context), patch(
                               "starforge_cues.printer_mqtt.socket.create_connection",
                               side_effect=failure):
                    with self.assertRaises(CollectorError) as caught:
                        _open_tls("192.168.1.2", Path("synthetic-ca"))
                self.assertEqual(diagnostic_code(caught.exception), expected)

        raw = Mock()
        for failure, expected in (
                (ssl.SSLCertVerificationError(1, "certificate verify failed"),
                 "tls_ca_validation_failed"),
                (socket.timeout(), "tls_timeout"),
                (ssl.SSLError(1, "handshake failed"), "tls_handshake_failed")):
            with self.subTest(expected=expected):
                context.wrap_socket.side_effect = failure
                with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                           return_value=context), patch(
                               "starforge_cues.printer_mqtt.socket.create_connection",
                               return_value=raw):
                    with self.assertRaises(CollectorError) as caught:
                        _open_tls("192.168.1.2", Path("synthetic-ca"))
                self.assertEqual(diagnostic_code(caught.exception), expected)
                raw.close.assert_called()

    def test_cli_probe_prints_only_bounded_category(self):
        from starforge_cues.cli import main
        output = io.StringIO()
        with patch("starforge_cues.printer_mqtt.probe_certificate",
                   side_effect=CollectorError("tls_ca_validation_failed")):
            with contextlib.redirect_stderr(output):
                status = main(["printer-cert-probe", "--host", "192.168.1.2",
                               "--ca-file", "private/path/to/ca"])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue().strip(),
                         "printer probe failed: tls_ca_validation_failed")
        self.assertEqual(diagnostic_code(CollectorError("private/path/to/secret")),
                         "mqtt_protocol_or_report_error")

    def test_x509_verify_code_is_bounded_and_never_exposes_raw_reason(self):
        failure = ssl.SSLCertVerificationError(1, "private/path/and/raw/cert/reason")
        failure.verify_code = 79
        context = Mock()
        context.wrap_socket.side_effect = failure
        raw = Mock()
        with patch("starforge_cues.printer_mqtt.ssl.create_default_context",
                   return_value=context), patch(
                       "starforge_cues.printer_mqtt.socket.create_connection",
                       return_value=raw):
            with self.assertRaises(CollectorError) as caught:
                _open_tls("192.168.1.2", Path("synthetic-ca"))
        self.assertEqual(diagnostic_code(caught.exception),
                         "tls_ca_validation_failed_x509_79")
        self.assertEqual(diagnostic_code(CollectorError(
            "tls_ca_validation_failed", verify_code=10000)), "tls_ca_validation_failed")
        self.assertEqual(diagnostic_code(CollectorError(
            "tls_ca_validation_failed", verify_code=True)), "tls_ca_validation_failed")
        from starforge_cues.cli import main
        output = io.StringIO()
        with patch("starforge_cues.printer_mqtt.probe_certificate",
                   side_effect=caught.exception), contextlib.redirect_stderr(output):
            status = main(["printer-cert-probe", "--host", "192.168.1.2",
                           "--ca-file", "private/path/to/ca"])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue().strip(),
                         "printer probe failed: tls_ca_validation_failed_x509_79")


class SessionSummaryTests(unittest.TestCase):
    def run_session(self, messages, *, interrupt=False, idle_after=0):
        clock = Clock()
        normalizer = P1ReportNormalizer("01P0000000000000", clock=lambda: clock.value,
                                        monotonic=lambda: clock.value)
        summaries = []
        events = []

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
                for message in messages:
                    yield message
                    clock.tick(2)
                clock.tick(idle_after)
                if interrupt:
                    clock.tick(1)
                    raise KeyboardInterrupt()

        if interrupt:
            with self.assertRaises(KeyboardInterrupt):
                collect_once(FakeClient(), normalizer, events.append,
                             on_summary=summaries.append, monotonic=lambda: clock.value)
        else:
            collect_once(FakeClient(), normalizer, events.append,
                         on_summary=summaries.append, monotonic=lambda: clock.value)
        self.assertEqual(len(summaries), 1)
        return summaries[0], events

    def test_no_messages_reports_authenticated_but_unobserved(self):
        summary, events = self.run_session([], interrupt=True)
        self.assertEqual(summary["exit"], "interrupted")
        self.assertTrue(summary["tls_verified"])
        self.assertTrue(summary["peer_pin_verified"])
        self.assertTrue(summary["auth_accepted"])
        self.assertTrue(summary["subscription_accepted"])
        self.assertEqual(summary["report_count"], 0)
        self.assertEqual(summary["normalized_state_count"], 0)
        self.assertIsNone(summary["last_generic_state"])
        self.assertIsNone(summary["last_state_age_seconds"])
        self.assertEqual(events, [])

    def test_partial_and_unsupported_reports_have_bounded_reasons(self):
        summary, events = self.run_session([
            b'{"other":"private-text"}', b'{"print":{"mc_percent":42}}',
            report("FAILED"), report("RUNNING", None), b'{"print":',
        ])
        self.assertEqual(summary["report_count"], 5)
        self.assertEqual(summary["normalized_state_count"], 0)
        self.assertEqual(summary["last_generic_state"], "unknown")
        self.assertEqual(summary["reason_counts"], {
            "invalid_report": 1, "no_print_report": 1, "no_state": 1, "unknown": 2,
        })
        self.assertEqual(summary["issue_counts"], {
            "invalid_report": 1, "no_print_report": 1, "no_state": 1,
            "unsupported_state": 1, "missing_or_invalid_task": 1,
        })
        self.assertNotIn("private-text", repr(summary))
        self.assertEqual(events, [])

    def test_running_without_finish_and_terminal_snapshot_are_distinct(self):
        running, events = self.run_session([report("RUNNING")])
        self.assertEqual(running["normalized_state_count"], 1)
        self.assertEqual(running["last_generic_state"], "printing")
        self.assertTrue(running["last_state_fresh"])
        self.assertEqual(running["reason_counts"], {"active": 1})
        self.assertEqual(events, [])

        stale, _ = self.run_session([report("RUNNING")], idle_after=21)
        self.assertEqual(stale["last_state_age_seconds"], 23)
        self.assertFalse(stale["last_state_fresh"])

        deltas, events = self.run_session(
            [report("RUNNING")] + [b'{"print":{"mc_percent":42}}'] * 43)
        self.assertEqual(deltas["last_generic_state"], "printing")
        self.assertEqual(deltas["last_state_age_seconds"], 88)
        self.assertEqual(deltas["last_report_age_seconds"], 2)
        self.assertFalse(deltas["last_state_fresh"])
        self.assertEqual(deltas["issue_counts"], {"no_state": 43})
        self.assertEqual(events, [])

        terminal, events = self.run_session([report("FINISH")])
        self.assertEqual(terminal["last_generic_state"], "finished")
        self.assertEqual(terminal["reason_counts"], {"unproven_terminal": 1})
        self.assertEqual(terminal["event_count"], 0)
        self.assertEqual(events, [])

        proven, events = self.run_session([report("RUNNING"), report("FINISH")])
        self.assertEqual(proven["normalized_state_count"], 2)
        self.assertEqual(proven["event_count"], 1)
        self.assertEqual([event.cue_id for event in events], ["printer.completed"])
        self.assertNotIn("synthetic-task", repr(proven))

    def test_failed_mqtt_auth_reports_stage_without_claiming_subscription(self):
        clock = Clock()
        normalizer = P1ReportNormalizer("01P0000000000000", clock=lambda: clock.value,
                                        monotonic=lambda: clock.value)
        summaries = []

        class RejectingClient:
            tls_verified = True
            peer_pin_verified = True
            auth_accepted = False
            subscription_accepted = False

            def __enter__(self):
                raise CollectorError("MQTT connection rejected")

            def __exit__(self, *_):
                pass

        with self.assertRaises(CollectorError):
            collect_once(RejectingClient(), normalizer, lambda _: None,
                         on_summary=summaries.append, monotonic=lambda: clock.value)
        self.assertEqual(summaries[0]["exit"], "mqtt_connection_rejected")
        self.assertTrue(summaries[0]["tls_verified"])
        self.assertFalse(summaries[0]["auth_accepted"])
        self.assertFalse(summaries[0]["subscription_accepted"])
        self.assertEqual(summaries[0]["report_count"], 0)


if __name__ == "__main__":
    unittest.main()
