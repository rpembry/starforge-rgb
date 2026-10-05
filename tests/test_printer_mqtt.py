"""Synthetic P1 MQTT and credential tests; no network, printer, or real secret."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest

from starforge_cues.printer_mqtt import (
    CollectorError, P1ReportNormalizer, SubscribeOnlyP1Client, _packet,
    _read_packet, collect_once, parse_fingerprint, read_private_access_code,
    validate_address,
)


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
        self.clock.tick(21)
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


if __name__ == "__main__":
    unittest.main()
