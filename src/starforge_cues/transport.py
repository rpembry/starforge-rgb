"""Optional local Unix socket adapter; core remains OS independent."""

import json
import os
from pathlib import Path
import socket
import socketserver
import stat
from threading import BoundedSemaphore
from typing import Callable

from .contract import ContractError, CueEvent, MAX_BYTES
from .core import Coordinator, FakeSink, CHANNELS

_RECEIPT_REASONS = frozenset({
    "capacity", "cooldown", "duplicate", "feedback_loop", "future",
    "lease_limit", "rate_limit", "replay_conflict", "self_origin",
    "source_capability", "source_limit", "stale", "receiver_scope",
})
_CHANNEL_OUTCOMES = frozenset({
    "accepted", "failed", "unknown", "unsupported", "absent", "suppressed",
    "preempted",
})


def to_receipt(event: CueEvent, internal: dict) -> dict:
    """Project only the caller's outcome; never return an internal render plan."""
    result = internal.get("result")
    if not isinstance(result, str) or result not in {"accepted", "suppressed", "rejected"}:
        result = "rejected"
    reason = internal.get("reason")
    if reason is not None and (not isinstance(reason, str) or reason not in _RECEIPT_REASONS):
        reason = "internal"
    channels = internal.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}
    # Cancellation reconciliation belongs to whichever other lease became
    # visible; it is never part of this producer's receipt.
    if event.status == "cancelled" or (channels and all(
            value == "preempted" for value in channels.values())):
        channels = {}
    else:
        channels = {channel: channels[channel] for channel in CHANNELS
                    if isinstance(channels.get(channel), str) and
                    channels[channel] in _CHANNEL_OUTCOMES}
    return {"version": 1, "event_id": event.event_id, "result": result,
            "reason": reason, "channels": channels}


def default_socket() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError("XDG_RUNTIME_DIR is required; pass --socket for an explicit local path")
    return Path(runtime) / "starforge-cues" / "cue.sock"


def _safe_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("socket parent cannot be a symlink")
    if not path.exists():
        path.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("socket parent must be owned by the current user and mode 0700")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(2)
            raw = self.rfile.readline(MAX_BYTES + 2)
            if not raw.endswith(b"\n") or len(raw) > MAX_BYTES + 1:
                raise ContractError("event: too large or unterminated")
            event = CueEvent.from_json(raw[:-1])
            try:
                permitted = (self.server.event_filter is None or
                             self.server.event_filter(event) is True)
            except Exception:
                permitted = False
            if not permitted:
                internal = {"result": "rejected", "reason": "receiver_scope", "channels": {}}
            else:
                internal = self.server.coordinator.handle(event)
            response = to_receipt(event, internal)
        except ContractError:
            response = {"version": 1, "event_id": None, "result": "rejected",
                        "reason": "invalid_event", "channels": {}}
        except (OSError, TimeoutError):
            return
        try:
            self.wfile.write(json.dumps(response, sort_keys=True).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


class LocalServer(socketserver.ThreadingMixIn, getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, path: Path, coordinator: Coordinator | None = None,
                 event_filter: Callable[[CueEvent], bool] | None = None):
        if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
            raise RuntimeError("restricted Unix socket hosting is unavailable on this platform")
        if event_filter is not None and not callable(event_filter):
            raise ValueError("invalid receiver scope")
        path = Path(path)
        _safe_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise RuntimeError("socket path already exists; stop the other server first")
        self.coordinator = coordinator or Coordinator({channel: FakeSink() for channel in CHANNELS})
        self.event_filter = event_filter
        self._slots = BoundedSemaphore(16)
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)
        info = path.lstat()
        self._socket_identity = (info.st_dev, info.st_ino)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def service_actions(self):
        self.coordinator.tick()

    def server_close(self):
        super().server_close()
        path = Path(self.server_address)
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._socket_identity:
                path.unlink()
        except FileNotFoundError:
            pass

    def close(self):
        """Explicitly close and remove only this server's own socket inode."""
        self.server_close()


def submit(path: Path, raw: bytes) -> dict:
    # Reference client validates before transport and never logs event contents.
    CueEvent.from_json(raw)
    packet = json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(packet) > MAX_BYTES:
        raise ContractError("event: too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(path))
        conn.sendall(packet + b"\n")
        result = b""
        while not result.endswith(b"\n") and len(result) <= MAX_BYTES:
            chunk = conn.recv(4096)
            if not chunk:
                break
            result += chunk
    return json.loads(result)
