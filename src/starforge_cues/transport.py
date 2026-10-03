"""Optional local Unix socket adapter; core remains OS independent."""

import json
import os
from pathlib import Path
import socket
import socketserver
import stat

from .contract import ContractError, CueEvent, MAX_BYTES
from .core import Coordinator, FakeSink, CHANNELS


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
        raw = self.rfile.readline(MAX_BYTES + 2)
        try:
            if not raw.endswith(b"\n") or len(raw) > MAX_BYTES + 1:
                raise ContractError("event: too large or unterminated")
            event = CueEvent.from_json(raw[:-1])
            response = self.server.coordinator.handle(event)
        except ContractError as exc:
            response = {"result": "rejected", "reason": str(exc), "channels": {}}
        self.wfile.write(json.dumps(response, sort_keys=True).encode() + b"\n")


class LocalServer(socketserver.UnixStreamServer):
    def __init__(self, path: Path, coordinator: Coordinator | None = None):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix domain sockets are unavailable on this platform")
        path = Path(path)
        _safe_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise RuntimeError("socket path already exists; stop the other server first")
        self.coordinator = coordinator or Coordinator({channel: FakeSink() for channel in CHANNELS})
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)


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
