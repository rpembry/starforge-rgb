"""Local account setting and opt-in printer-to-bound-listener adapter."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import stat

try:
    import pwd
except ImportError:  # Portable core CI imports this host adapter on Windows.
    pwd = None

from .contract import CueEvent, MAX_BYTES
from .ingress_contract import IngressBinding
from .printer_mqtt import P1ReportNormalizer
from .registered_host import RegisteredHost
from .core import Coordinator


_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
MAX_SETTINGS_BYTES = 1024


class IngressSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class PrinterIngressSettings:
    allowed_account: str
    runtime_root: Path


def parse_printer_ingress_settings(raw: bytes) -> PrinterIngressSettings:
    if not isinstance(raw, bytes) or len(raw) > MAX_SETTINGS_BYTES:
        raise IngressSettingsError("invalid ingress settings size")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IngressSettingsError("duplicate ingress setting")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IngressSettingsError("invalid ingress settings JSON") from exc
    if (not isinstance(value, dict) or value.keys() !=
            {"version", "allowed_account", "runtime_root"} or
            type(value["version"]) is not int or value["version"] != 1):
        raise IngressSettingsError("invalid ingress settings fields")
    account = value["allowed_account"]
    root = value["runtime_root"]
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise IngressSettingsError("invalid existing account name")
    if (not isinstance(root, str) or len(root) > 256 or not root.startswith("/") or
            ".." in Path(root).parts or root == "/"):
        raise IngressSettingsError("invalid runtime root")
    return PrinterIngressSettings(account, Path(root))


def read_private_printer_ingress_settings(path: Path) -> PrinterIngressSettings:
    """Read only an owned, single-link 0600 file in an owned 0700 directory."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise IngressSettingsError("private settings unsupported")
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(path.parent, flags | os.O_DIRECTORY)
        try:
            parent = os.fstat(parent_fd)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or
                    stat.S_IMODE(parent.st_mode) != 0o700):
                raise IngressSettingsError("ingress settings directory is not private")
            file_fd = os.open(path.name, flags, dir_fd=parent_fd)
            try:
                before = os.fstat(file_fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                        before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600 or
                        before.st_size > MAX_SETTINGS_BYTES):
                    raise IngressSettingsError("ingress settings file is not private")
                raw = os.read(file_fd, MAX_SETTINGS_BYTES + 1)
                after = os.fstat(file_fd)
                if (len(raw) != before.st_size or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                         before.st_ctime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                         after.st_ctime_ns)):
                    raise IngressSettingsError("ingress settings changed during read")
            finally:
                os.close(file_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise IngressSettingsError("private ingress settings unavailable") from exc
    return parse_printer_ingress_settings(raw)


def allowed_uid(settings: PrinterIngressSettings) -> int:
    """Resolve an existing Linux account; never create or grant one."""
    if not isinstance(settings, PrinterIngressSettings):
        raise TypeError("host settings required")
    if pwd is None or not hasattr(os, "geteuid"):
        raise IngressSettingsError("local account lookup unavailable")
    try:
        uid = pwd.getpwnam(settings.allowed_account).pw_uid
    except KeyError as exc:
        raise IngressSettingsError("allowed account does not exist") from exc
    if uid != os.geteuid():
        raise IngressSettingsError("allowed account differs from running host")
    return uid


def printer_binding(normalizer: P1ReportNormalizer) -> IngressBinding:
    if not isinstance(normalizer, P1ReportNormalizer):
        raise TypeError("printer normalizer required")
    return IngressBinding("printer.local", frozenset({normalizer.adapter.source_id}),
                          frozenset({"succeeded", "failed"}), "warning", "known",
                          True, True, False)


def registered_printer_host(settings: PrinterIngressSettings,
                            normalizer: P1ReportNormalizer,
                            coordinator: Coordinator) -> RegisteredHost:
    """Resolve local account before opening the single-source listener."""
    allowed_uid(settings)
    return RegisteredHost(settings.runtime_root, printer_binding(normalizer), coordinator)


class BoundPrinterPublisher:
    """Send semantic CueEvents only; no MQTT or output sink access."""

    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)

    def _request(self, frame: dict) -> dict:
        packet = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode()
        if len(packet) > MAX_BYTES:
            raise ValueError("printer frame too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(str(self.socket_path))
            conn.sendall(packet + b"\n")
            response = b""
            while not response.endswith(b"\n") and len(response) <= MAX_BYTES:
                block = conn.recv(4096)
                if not block:
                    break
                response += block
        if not response.endswith(b"\n") or len(response) > MAX_BYTES:
            raise ValueError("invalid printer ingress receipt")
        result = json.loads(response)
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError("invalid printer ingress receipt")
        return result

    def publish(self, event: CueEvent) -> dict:
        if not isinstance(event, CueEvent):
            raise TypeError("semantic printer event required")
        hello = self._request({"proto": 1, "op": "hello"})
        epoch = hello.get("session_epoch")
        if hello.get("result") != "ready" or not isinstance(epoch, str):
            raise ValueError("printer ingress session unavailable")
        body = {**event.__dict__, "occurred_at": event.occurred_at.isoformat(),
                "observed_at": event.observed_at.isoformat()}
        result = self._request({"proto": 1, "op": "publish", "session_epoch": epoch,
                                "event": body})
        if result.get("event_id") != event.event_id or result.get("result") not in {
                "accepted", "suppressed", "rejected"}:
            raise ValueError("invalid printer ingress receipt")
        return result
