"""Opt-in Linux UDS publish path with a host-owned, peer-UID binding."""

import json
import socket
import struct
import sys

from .contract import ContractError, MAX_BYTES
from .core import Coordinator
from .ingress_contract import HelloFrame, IngressBinding, PublishFrame, authorize_frame, parse_frame_json
from .transport import LocalServer, to_receipt


class _BoundHandler(LocalServer.handler_class):
    def handle(self) -> None:
        try:
            self.request.settimeout(2)
            if self.server.peer_uid(self.request) != self.server.expected_uid:
                response = _rejected("unauthorized_peer")
            else:
                raw = self.rfile.readline(MAX_BYTES + 2)
                if not raw.endswith(b"\n") or len(raw) > MAX_BYTES + 1:
                    raise ContractError("frame: too large or unterminated")
                frame = parse_frame_json(raw[:-1], expected_session=self.server.session_epoch)
                if isinstance(frame, HelloFrame) and self.server.session_epoch is not None:
                    response = {"version": 1, "result": "ready",
                                "session_epoch": self.server.session_epoch}
                elif not isinstance(frame, PublishFrame):
                    response = _rejected("unsupported_operation")
                else:
                    try:
                        authorize_frame(self.server.binding, frame)
                    except ContractError:
                        response = _rejected("receiver_scope")
                    else:
                        response = to_receipt(frame.event, self.server.coordinator.handle(frame.event))
        except ContractError:
            response = _rejected("invalid_event")
        except (OSError, TimeoutError):
            return
        try:
            self.wfile.write(json.dumps(response, sort_keys=True).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def _rejected(reason: str) -> dict:
    return {"version": 1, "event_id": None, "result": "rejected",
            "reason": reason, "channels": {}}


class BoundLocalServer(LocalServer):
    """One source per restricted listener; no CLI, service, or real sink setup."""

    handler_class = _BoundHandler

    def __init__(self, path, coordinator: Coordinator, binding: IngressBinding,
                 *, expected_uid: int, session_epoch: str | None = None):
        if (sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED") or
                not isinstance(binding, IngressBinding) or
                not isinstance(coordinator, Coordinator) or
                type(expected_uid) is not int or expected_uid < 0 or
                (session_epoch is not None and
                 (not isinstance(session_epoch, str) or len(session_epoch) != 32 or
                  any(char not in "0123456789abcdef" for char in session_epoch))) or
                len(binding.source_ids) != 1 or binding.can_retract or
                "cancelled" in binding.statuses):
            raise ValueError("bound publish listener requires Linux, one source and no retraction")
        self.binding = binding
        self.expected_uid = expected_uid
        self.session_epoch = session_epoch
        super().__init__(path, coordinator)

    @staticmethod
    def peer_uid(conn: socket.socket) -> int:
        size = struct.calcsize("3i")
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        if len(data) != size:
            raise OSError("missing peer credentials")
        _pid, uid, _gid = struct.unpack("3i", data)
        return uid
