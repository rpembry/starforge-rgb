"""Foreground, subscribe-only P1 status collector. No printer control API."""

from datetime import datetime, timezone
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import struct
import time
from typing import Callable, Iterator

from .contract import CueEvent
from .printer_status import BambuCompletionAdapter, PrinterDecision, PrinterObservation

MAX_REPORT = 16384
MAX_PACKET_GAP_S = 60
MAX_STATE_LEASE_S = 24 * 60 * 60
_SERIAL = re.compile(r"[A-Za-z0-9]{12,32}\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_FINGERPRINT = re.compile(r"[0-9a-fA-F]{64}\Z")
_PRIVATE_NETS = tuple(ipaddress.ip_network(value) for value in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class CollectorError(ValueError):
    """A generic setup or protocol error that never includes private values."""

    def __init__(self, message: str, *, verify_code: int | None = None):
        super().__init__(message)
        self.verify_code = verify_code if type(verify_code) is int and 1 <= verify_code <= 999 else None


_DIAGNOSTICS = {
    "printer address must be a private IPv4 address": "invalid_private_ipv4",
    "invalid printer serial": "invalid_serial",
    "invalid peer certificate fingerprint": "invalid_peer_pin",
    "printer certificate unavailable": "peer_certificate_missing",
    "printer certificate pin mismatch": "peer_pin_mismatch",
    "private credential unavailable": "private_credential_unavailable",
    "credential directory must be owned and mode 0700": "private_credential_permissions",
    "credential file must be private and bounded": "private_credential_permissions",
    "credential changed during read": "private_credential_changed",
    "invalid credential format": "invalid_credential_format",
    "MQTT connection rejected": "mqtt_connection_rejected",
    "MQTT subscription rejected": "mqtt_subscription_rejected",
}


def diagnostic_code(exc: Exception) -> str:
    """Return only fixed categories, never exception details or host paths."""
    if isinstance(exc, CollectorError):
        message = str(exc)
        if message == "tls_ca_validation_failed" and exc.verify_code is not None:
            return f"tls_ca_validation_failed_x509_{exc.verify_code}"
        if message in _DIAGNOSTICS:
            return _DIAGNOSTICS[message]
        if message in {
                "ca_file_missing", "ca_file_unreadable", "ca_file_invalid",
                "ca_file_unavailable",
                "connection_refused", "connection_timeout", "network_unreachable",
                "connection_failed", "tls_ca_validation_failed", "tls_timeout",
                "tls_handshake_failed", "tls_transport_failed"}:
            return message
        return "mqtt_protocol_or_report_error"
    if isinstance(exc, ImportError):
        return "dependency_unavailable"
    return "unexpected_local_error"


def validate_address(host: str, serial: str) -> tuple[str, str]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise CollectorError("printer address must be a private IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not any(
            address in network for network in _PRIVATE_NETS):
        raise CollectorError("printer address must be a private IPv4 address")
    if not isinstance(serial, str) or not _SERIAL.fullmatch(serial):
        raise CollectorError("invalid printer serial")
    return str(address), serial


def parse_fingerprint(value: str) -> bytes:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise CollectorError("invalid peer certificate fingerprint")
    return bytes.fromhex(value)


def default_credential_path() -> Path:
    return Path.home() / ".config/starforge-rgb/credentials/bambu-p1s-access-code"


def read_private_access_code(path: Path) -> bytes:
    """Read one bounded secret from an owned private directory, without symlinks."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise CollectorError("private credential files unsupported on this host")
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        directory_fd = os.open(path.parent, flags | os.O_DIRECTORY)
        try:
            directory = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or
                    stat.S_IMODE(directory.st_mode) != 0o700):
                raise CollectorError("credential directory must be owned and mode 0700")
            file_fd = os.open(path.name, flags, dir_fd=directory_fd)
            try:
                before = os.fstat(file_fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                        before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in (0o400, 0o600) or
                        not 8 <= before.st_size <= 10):
                    raise CollectorError("credential file must be private and bounded")
                raw = os.read(file_fd, 11)
                after = os.fstat(file_fd)
                if (len(raw) != before.st_size or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                         before.st_ctime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                         after.st_ctime_ns)):
                    raise CollectorError("credential changed during read")
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CollectorError("private credential unavailable") from exc
    code = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    if len(code) != 8 or not code.isalnum() or not code.isascii():
        raise CollectorError("invalid credential format")
    return code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectorError("duplicate report field")
        result[key] = value
    return result


class P1ReportNormalizer:
    """Merge harmless deltas within one bounded session; require explicit transitions."""

    def __init__(self, serial: str, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        if not isinstance(serial, str) or not _SERIAL.fullmatch(serial):
            raise CollectorError("invalid printer serial")
        source = "printer.p1s-" + hashlib.sha256(serial.encode("ascii")).hexdigest()[:16]
        self.adapter = BambuCompletionAdapter(source, clock=clock)
        self.clock = clock
        self.monotonic = monotonic
        self.epoch = 0
        self.sequence = 0
        self.last_state_at: float | None = None
        self.last_known_state: str | None = None
        self.last_input_at: float | None = None
        self.last_input_issue: str | None = None
        self.normalized_state_count = 0
        self.is_connected = False

    def connected(self) -> None:
        if self.is_connected:
            raise CollectorError("collector is already connected")
        if self.epoch >= 2**31 - 1:
            raise CollectorError("connection epoch exhausted")
        self.epoch += 1
        self.sequence = 0
        self.last_state_at = None
        self.last_known_state = None
        self.last_input_at = None
        self.last_input_issue = None
        self.normalized_state_count = 0
        self.is_connected = True

    def disconnected(self) -> None:
        if self.is_connected:
            # Erase the adapter's active-job proof before any delayed callback
            # can arrive. The next connection also starts a new epoch.
            self._observe("unknown", None)
            self.is_connected = False
        self.last_state_at = None

    def _observe(self, state: str, job_id: str | None) -> PrinterDecision:
        self.sequence += 1
        instant = datetime.fromtimestamp(self.clock(), timezone.utc)
        return self.adapter.observe(PrinterObservation(
            self.epoch, self.sequence, instant, state, job_id))

    def accept(self, raw: bytes) -> PrinterDecision:
        if not self.is_connected:
            raise CollectorError("collector is disconnected")
        now = self.monotonic()
        self.last_input_issue = None
        if not isinstance(raw, bytes) or len(raw) > MAX_REPORT:
            self.last_state_at = None
            self.last_known_state, self.last_input_at = "unknown", now
            self.last_input_issue = "invalid_report"
            self._observe("unknown", None)
            raise CollectorError("report exceeds size bound")
        if ((self.last_input_at is not None and
             (now < self.last_input_at or now - self.last_input_at > MAX_PACKET_GAP_S)) or
            (self.last_state_at is not None and
             (now < self.last_state_at or now - self.last_state_at > MAX_STATE_LEASE_S))):
            self._observe("unknown", None)
            self.last_state_at = None
            self.last_known_state = "unknown"
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError) as exc:
            self.last_state_at = None
            self.last_known_state, self.last_input_at = "unknown", now
            self.last_input_issue = "invalid_report"
            self._observe("unknown", None)
            raise CollectorError("invalid report JSON") from exc
        if not isinstance(data, dict) or ("print" in data and
                                           not isinstance(data["print"], dict)):
            self.last_state_at = None
            self.last_known_state, self.last_input_at = "unknown", now
            self.last_input_issue = "invalid_print_shape"
            return self._observe("unknown", None)
        if "print" not in data:
            self.last_input_at = now
            self.last_input_issue = "no_print_report"
            return PrinterDecision("suppressed", "no_print_report")
        report = data["print"]
        raw_state = report.get("gcode_state")
        if raw_state is None:
            # Sparse push_status deltas do not assert a new printer state.
            self.last_input_at = now
            if "task_id" in report and self.adapter.active_job_id is not None:
                task_delta = report["task_id"]
                if (not isinstance(task_delta, str) or not _TASK.fullmatch(task_delta) or
                        "j" + hashlib.sha256(task_delta.encode("ascii")).hexdigest()[:32]
                        != self.adapter.active_job_id):
                    self.last_state_at = None
                    self.last_known_state = "unknown"
                    self.last_input_issue = "task_changed_without_state"
                    return self._observe("unknown", None)
            self.last_input_issue = "no_state"
            return PrinterDecision("suppressed", "no_state")
        if not isinstance(raw_state, str):
            self.last_state_at = None
            self.last_known_state, self.last_input_at = "unknown", now
            self.last_input_issue = "invalid_state_type"
            return self._observe("unknown", None)
        state = {"RUNNING": "printing", "PAUSE": "paused", "FINISH": "finished",
                 "IDLE": "idle"}.get(raw_state, "unknown")
        if state == "unknown":
            self.last_input_issue = "unsupported_state"
        # P1S failure is not documented by the integration's device triggers.
        # FAILED, cancellation, and all other states stay unknown pending evidence.
        task = report.get("task_id")
        if state in ("printing", "paused", "finished"):
            if not isinstance(task, str) or not _TASK.fullmatch(task):
                state = "unknown"
                task = None
                self.last_input_issue = "missing_or_invalid_task"
            else:
                task = "j" + hashlib.sha256(task.encode("ascii")).hexdigest()[:32]
        else:
            task = None
        self.last_known_state, self.last_input_at = state, now
        if state != "unknown":
            self.normalized_state_count += 1
        self.last_state_at = now if state != "unknown" else None
        return self._observe(state, task)


def _encoded_string(raw: bytes) -> bytes:
    if len(raw) > 65535:
        raise CollectorError("MQTT field too large")
    return struct.pack("!H", len(raw)) + raw


def _remaining_length(length: int) -> bytes:
    if not 0 <= length <= MAX_REPORT + 256:
        raise CollectorError("MQTT packet too large")
    result = bytearray()
    while True:
        digit = length % 128
        length //= 128
        result.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(result)


def _packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _remaining_length(len(body)) + body


def _read_exact(stream, count: int, *, idle_timeout: bool = False) -> bytes:
    result = bytearray()
    while len(result) < count:
        try:
            chunk = stream.recv(count - len(result))
        except socket.timeout as exc:
            if idle_timeout and not result:
                raise
            raise CollectorError("incomplete MQTT packet") from exc
        if not chunk:
            raise CollectorError("MQTT connection closed")
        result.extend(chunk)
    return bytes(result)


def _read_packet(stream) -> tuple[int, bytes]:
    header = _read_exact(stream, 1, idle_timeout=True)[0]
    size = 0
    factor = 1
    for _ in range(4):
        digit = _read_exact(stream, 1)[0]
        size += (digit & 127) * factor
        if size > MAX_REPORT + 256:
            raise CollectorError("MQTT packet too large")
        if not digit & 128:
            return header, _read_exact(stream, size)
        factor *= 128
    raise CollectorError("invalid MQTT length")


def _open_tls(host: str, ca_file: Path, *, bambu_legacy_ca: bool = False):
    # CA chain validation is required. A separate exact leaf pin replaces DNS
    # hostname validation because the printer is addressed by LAN IP.
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
    except FileNotFoundError as exc:
        raise CollectorError("ca_file_missing") from exc
    except PermissionError as exc:
        raise CollectorError("ca_file_unreadable") from exc
    except (ssl.SSLError, ValueError) as exc:
        raise CollectorError("ca_file_invalid") from exc
    except OSError as exc:
        raise CollectorError("ca_file_unavailable") from exc
    if bambu_legacy_ca is True:
        # Explicit printer-only compatibility for Bambu CA certificates that
        # fail Python 3.13+ strict X.509 profile checks. CA verification stays on.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    try:
        raw = socket.create_connection((host, 8883), timeout=15)
    except ConnectionRefusedError as exc:
        raise CollectorError("connection_refused") from exc
    except TimeoutError as exc:
        raise CollectorError("connection_timeout") from exc
    except OSError as exc:
        code = ("network_unreachable" if exc.errno in
                {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EHOSTDOWN}
                else "connection_failed")
        raise CollectorError(code) from exc
    try:
        wrapped = context.wrap_socket(raw, server_hostname=host)
        wrapped.settimeout(15)
        return wrapped
    except Exception as exc:
        raw.close()
        if isinstance(exc, ssl.SSLCertVerificationError):
            raise CollectorError("tls_ca_validation_failed",
                                 verify_code=getattr(exc, "verify_code", None)) from exc
        if isinstance(exc, TimeoutError):
            raise CollectorError("tls_timeout") from exc
        if isinstance(exc, ssl.SSLError):
            raise CollectorError("tls_handshake_failed") from exc
        if isinstance(exc, OSError):
            raise CollectorError("tls_transport_failed") from exc
        raise


def probe_certificate(host: str, ca_file: Path, *, bambu_legacy_ca: bool = False,
                      tls_opener=_open_tls) -> str:
    """Return a CA-validated leaf fingerprint without reading credentials."""
    host, _ = validate_address(host, "000000000000")
    with tls_opener(host, ca_file, **({"bambu_legacy_ca": True} if bambu_legacy_ca is True else {})) as stream:
        certificate = stream.getpeercert(binary_form=True)
        if not certificate:
            raise CollectorError("printer certificate unavailable")
        return hashlib.sha256(certificate).hexdigest()


class SubscribeOnlyP1Client:
    """MQTT 3.1.1 subset: CONNECT, SUBSCRIBE, PINGREQ, DISCONNECT only."""

    def __init__(self, host: str, serial: str, ca_file: Path, peer_sha256: str,
                 credential_file: Path, *, tls_opener=_open_tls,
                 credential_loader=read_private_access_code,
                 bambu_legacy_ca: bool = False):
        self.host, self.serial = validate_address(host, serial)
        self.ca_file = Path(ca_file)
        self.peer_pin = parse_fingerprint(peer_sha256)
        self.credential_file = Path(credential_file)
        self.tls_opener = tls_opener
        self.credential_loader = credential_loader
        self.bambu_legacy_ca = bambu_legacy_ca
        self.stream = None
        self.tls_verified = False
        self.peer_pin_verified = False
        self.auth_accepted = False
        self.subscription_accepted = False
        self.topic = f"device/{self.serial}/report".encode("ascii")

    def __enter__(self):
        self.tls_verified = False
        self.peer_pin_verified = False
        self.auth_accepted = False
        self.subscription_accepted = False
        stream = self.tls_opener(
            self.host, self.ca_file,
            **({"bambu_legacy_ca": True} if self.bambu_legacy_ca is True else {}))
        self.tls_verified = True
        try:
            certificate = stream.getpeercert(binary_form=True)
            if not certificate or not secrets.compare_digest(
                    hashlib.sha256(certificate).digest(), self.peer_pin):
                raise CollectorError("printer certificate pin mismatch")
            self.peer_pin_verified = True
            code = self.credential_loader(self.credential_file)
            client_id = b"starforge-" + secrets.token_hex(8).encode("ascii")
            variable = _encoded_string(b"MQTT") + bytes((4, 0xC2)) + struct.pack("!H", 30)
            payload = (_encoded_string(client_id) + _encoded_string(b"bblp") +
                       _encoded_string(code))
            stream.sendall(_packet(0x10, variable + payload))
            header, body = _read_packet(stream)
            if header != 0x20 or body != b"\x00\x00":
                raise CollectorError("MQTT connection rejected")
            self.auth_accepted = True
            stream.sendall(_packet(0x82, b"\x00\x01" + _encoded_string(self.topic) + b"\x00"))
            header, body = _read_packet(stream)
            if header != 0x90 or body != b"\x00\x01\x00":
                raise CollectorError("MQTT subscription rejected")
            self.subscription_accepted = True
            self.stream = stream
            return self
        except Exception:
            stream.close()
            raise

    def __exit__(self, *_):
        if self.stream is not None:
            try:
                self.stream.sendall(b"\xe0\x00")
            except OSError:
                pass
            self.stream.close()
            self.stream = None

    def reports(self) -> Iterator[bytes]:
        if self.stream is None:
            raise CollectorError("MQTT subscription is closed")
        pending_ping = False
        while True:
            try:
                header, body = _read_packet(self.stream)
            except socket.timeout as exc:
                if pending_ping:
                    raise CollectorError("MQTT heartbeat timed out") from exc
                self.stream.sendall(b"\xc0\x00")
                pending_ping = True
                continue
            if header == 0xD0 and body == b"":
                pending_ping = False
                continue
            if header != 0x30 or len(body) < 2:
                raise CollectorError("unexpected MQTT packet")
            topic_len = struct.unpack("!H", body[:2])[0]
            if len(body) < 2 + topic_len:
                raise CollectorError("invalid MQTT report topic")
            topic = body[2:2 + topic_len]
            if topic != self.topic:
                raise CollectorError("unexpected MQTT report topic")
            payload = body[2 + topic_len:]
            if len(payload) > MAX_REPORT:
                raise CollectorError("report exceeds size bound")
            pending_ping = False
            yield payload


_SUMMARY_REASONS = frozenset({
    "active", "paused", "idle", "unknown", "no_print_report", "no_state",
    "invalid_report", "unproven_terminal", "duplicate_terminal",
    "stale_or_future", "out_of_order",
})
_SUMMARY_ISSUES = frozenset({
    "invalid_report", "invalid_print_shape", "no_print_report", "no_state",
    "invalid_state_type", "unsupported_state", "missing_or_invalid_task",
    "task_changed_without_state",
})


def collect_once(client: SubscribeOnlyP1Client, normalizer: P1ReportNormalizer,
                 on_event: Callable[[CueEvent], None], *,
                 on_summary: Callable[[dict], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
    """One foreground session with bounded, content-free commissioning evidence."""
    started = monotonic()
    reports = 0
    events = 0
    reasons: dict[str, int] = {}
    issues: dict[str, int] = {}
    exit_reason = "stream_ended"

    def count_issue() -> None:
        issue = normalizer.last_input_issue
        if issue in _SUMMARY_ISSUES:
            issues[issue] = min(issues.get(issue, 0) + 1, 999999)

    try:
        with client:
            normalizer.connected()
            for raw in client.reports():
                reports = min(reports + 1, 999999)
                try:
                    decision = normalizer.accept(raw)
                except CollectorError:
                    count_issue()
                    reasons["invalid_report"] = min(reasons.get("invalid_report", 0) + 1,
                                                    999999)
                    continue
                count_issue()
                if decision.reason in _SUMMARY_REASONS:
                    reasons[decision.reason] = min(reasons.get(decision.reason, 0) + 1,
                                                   999999)
                if decision.event is not None:
                    events = min(events + 1, 999999)
                    on_event(decision.event)
    except KeyboardInterrupt:
        exit_reason = "interrupted"
        raise
    except Exception as exc:
        exit_reason = diagnostic_code(exc)
        raise
    finally:
        ended = monotonic()
        age_exact = (max(0, ended - normalizer.last_input_at)
               if normalizer.last_input_at is not None else None)
        age = min(999999, int(age_exact)) if age_exact is not None else None
        summary = {
            "tls_verified": getattr(client, "tls_verified", False) is True,
            "peer_pin_verified": getattr(client, "peer_pin_verified", False) is True,
            "auth_accepted": getattr(client, "auth_accepted", False) is True,
            "subscription_accepted": getattr(client, "subscription_accepted", False) is True,
            "report_count": reports,
            "normalized_state_count": min(normalizer.normalized_state_count, 999999),
            "event_count": events,
            "last_generic_state": normalizer.last_known_state,
            "last_state_age_seconds": (min(999999, int(max(0, ended - normalizer.last_state_at)))
                                       if normalizer.last_state_at is not None else None),
            "last_state_fresh": (normalizer.last_state_at is not None and
                                 0 <= ended - normalizer.last_state_at <= 20),
            "last_report_age_seconds": age,
            "reason_counts": dict(sorted(reasons.items())),
            "issue_counts": dict(sorted(issues.items())),
            "duration_seconds": max(0, min(999999, int(ended - started))),
            "exit": exit_reason,
        }
        normalizer.disconnected()
        if on_summary is not None:
            on_summary(summary)
