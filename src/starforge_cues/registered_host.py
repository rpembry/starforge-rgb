"""Opt-in single-producer host path; no service or live producer setup."""

from pathlib import Path
import os
import secrets
import stat

from .bound_transport import BoundLocalServer
from .core import Coordinator, SourceCapabilities
from .ingress_contract import IngressBinding


class RuntimePathError(ValueError):
    pass


def validate_runtime_root(root: Path) -> Path:
    """Require an explicit private directory under nonreplaceable ancestors."""
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or root == Path("/"):
        raise RuntimePathError("runtime root must be an absolute private directory")
    current = Path("/")
    for part in root.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimePathError("runtime ancestor unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimePathError("runtime ancestor is not a directory")
        if current == root:
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimePathError("runtime root is not private")
        elif info.st_uid not in (0, os.geteuid()):
            raise RuntimePathError("runtime ancestor has untrusted owner")
        elif stat.S_IMODE(info.st_mode) & 0o022:
            # A root-owned sticky /tmp is the only writable ancestor admitted.
            if info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
                raise RuntimePathError("runtime ancestor is writable")
    return root


class RegisteredHost:
    """Bind one source to the caller's existing coordinator and local listener."""

    def __init__(self, runtime_root: Path, binding: IngressBinding,
                 coordinator: Coordinator):
        if (not isinstance(binding, IngressBinding) or len(binding.source_ids) != 1 or
                binding.can_retract or "cancelled" in binding.statuses):
            raise ValueError("one publish-only host binding required")
        self.runtime_root = validate_runtime_root(runtime_root)
        self.binding = binding
        source_id = next(iter(binding.source_ids))
        capability = SourceCapabilities(binding.statuses, binding.offers_text,
                                        binding.offers_subject)
        if (not isinstance(coordinator, Coordinator) or
                coordinator.sources != {source_id: capability} or
                coordinator._seen or coordinator._leases or coordinator._retired_subjects or
                coordinator._rate or coordinator._terminal_rate or coordinator._feedback):
            raise ValueError("fresh coordinator with this exact source registry required")
        self.coordinator = coordinator
        # The epoch is a replay barrier, not a secret or a process credential.
        self.session_epoch = secrets.token_hex(16)
        self.listener = BoundLocalServer(self.runtime_root / f"{binding.binding_id}.sock",
                                         self.coordinator, binding,
                                         expected_uid=os.geteuid(),
                                         session_epoch=self.session_epoch)
