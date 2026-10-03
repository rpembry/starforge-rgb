"""Read-only, private POSIX host settings for baseline and quiet policy.

This adapter never creates or changes host files. The portable coordinator does
not import it, and callers decide when to recover or reload fake/live sinks.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import Coordinator, QuietPolicy, QuietWindow, _valid_baseline_text

MAX_CONFIG_BYTES = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")
_CHANNELS = frozenset({"text", "rgb", "audio"})


class ConfigError(ValueError):
    """Configuration is missing, unsafe, or malformed; no contents are exposed."""


@dataclass(frozen=True)
class HostConfig:
    generation: int
    cue_id: str | None
    text: str | None
    policy: QuietPolicy

    @classmethod
    def safe_default(cls) -> "HostConfig":
        return cls(1, None, None, QuietPolicy(quiet=True))


def _object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError("invalid configuration fields")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ConfigError("invalid boolean setting")
    return value


def _minute(value: object) -> int:
    if type(value) is not int or not 0 <= value < 1440:
        raise ConfigError("invalid quiet minute")
    return value


def parse_host_config(raw: bytes) -> HostConfig:
    """Parse a bounded versioned data document, with no scripts or output paths."""
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("invalid configuration size")
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigError("duplicate configuration key")
                result[key] = value
            return result
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("invalid configuration JSON") from exc
    data = _object(data, {"version", "baseline", "quiet"})
    if type(data["version"]) is not int or data["version"] != 1:
        raise ConfigError("unsupported configuration version")
    baseline = _object(data["baseline"], {"generation", "cue_id", "text"})
    generation = baseline["generation"]
    cue_id = baseline["cue_id"]
    text = baseline["text"]
    if type(generation) is not int or not 1 <= generation <= 2**63 - 1:
        raise ConfigError("invalid baseline generation")
    if cue_id is not None and (not isinstance(cue_id, str) or not _IDENTIFIER.fullmatch(cue_id)):
        raise ConfigError("invalid baseline cue")
    if not _valid_baseline_text(text) or (cue_id is None and text is not None):
        raise ConfigError("invalid baseline text")
    quiet = _object(data["quiet"], {"manual", "dnd", "muted", "timezone", "windows"})
    muted = quiet["muted"]
    if (not isinstance(muted, list) or len(muted) > 3 or
            any(not isinstance(channel, str) or channel not in _CHANNELS for channel in muted) or
            len(set(muted)) != len(muted)):
        raise ConfigError("invalid muted channels")
    zone_name = quiet["timezone"]
    if not isinstance(zone_name, str) or len(zone_name) > 64 or not _ZONE.fullmatch(zone_name):
        raise ConfigError("invalid timezone")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("unknown timezone") from exc
    windows = quiet["windows"]
    if not isinstance(windows, list) or len(windows) > 8:
        raise ConfigError("invalid quiet windows")
    parsed_windows = []
    for item in windows:
        item = _object(item, {"start_minute", "end_minute"})
        parsed_windows.append(QuietWindow(_minute(item["start_minute"]),
                                          _minute(item["end_minute"])))
    policy = QuietPolicy(quiet=_boolean(quiet["manual"]), dnd=_boolean(quiet["dnd"]),
                         muted=frozenset(muted), windows=tuple(parsed_windows),
                         local_timezone=zone)
    return HostConfig(generation, cue_id, text, policy)


def read_private_config(path: str | os.PathLike[str]) -> HostConfig:
    """Read an owned regular file from an owned private directory without symlinks."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ConfigError("private file loading unsupported on this host")
    target = Path(path)
    if target.name in ("", ".", ".."):
        raise ConfigError("invalid configuration path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | os.O_DIRECTORY
    try:
        directory_fd = os.open(target.parent, directory_flags)
    except OSError as exc:
        raise ConfigError("private configuration directory unavailable") from exc
    try:
        directory = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or
                stat.S_IMODE(directory.st_mode) & 0o077):
            raise ConfigError("configuration directory is not private")
        try:
            file_fd = os.open(target.name, flags, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise ConfigError("private configuration missing") from exc
        except OSError as exc:
            raise ConfigError("private configuration unavailable") from exc
        try:
            before = os.fstat(file_fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                    before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in (0o400, 0o600) or
                    before.st_size > MAX_CONFIG_BYTES):
                raise ConfigError("configuration file is not private")
            raw = os.read(file_fd, MAX_CONFIG_BYTES + 1)
            after = os.fstat(file_fd)
            if (len(raw) > MAX_CONFIG_BYTES or
                    (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ConfigError("configuration changed during read")
            return parse_host_config(raw)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def reload_private_config(path: str | os.PathLike[str], coordinator: Coordinator) -> dict:
    """Reject missing/invalid settings without mutating the running coordinator."""
    try:
        config = read_private_config(path)
        state = coordinator.set_host_configuration(config.generation, config.cue_id,
                                                   config.text, config.policy)
    except (ConfigError, ValueError):
        return {"result": "rejected", "reason": "invalid_config", "channels": {}}
    return {"result": "loaded", "generation": config.generation, "state": state}


def recover_private_config(path: str | os.PathLike[str], **coordinator_options) -> tuple[Coordinator, str]:
    """On startup, missing or invalid settings clear outputs under quiet policy."""
    try:
        config = read_private_config(path)
        status = "loaded"
    except ConfigError as exc:
        config = HostConfig.safe_default()
        status = "missing" if str(exc) == "private configuration missing" else "invalid"
    coordinator = Coordinator.recover_baseline(config.generation, config.cue_id,
                                               config.text, policy=config.policy,
                                               unconfigured_host=status != "loaded",
                                               **coordinator_options)
    return coordinator, status
