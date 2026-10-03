"""Bounded preview of data-only local theme directories; no activation or I/O output."""

from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from .contract import CueEvent

_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_CHANNELS = frozenset({"text", "rgb", "audio"})
MAX_MANIFEST = 64 * 1024
MAX_FILES = 32
MAX_TOTAL = 8 * 1024 * 1024
MAX_ASSET = 1024 * 1024


class ThemeError(ValueError):
    pass


def _fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        raise ThemeError("theme: invalid fields")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ThemeError("theme: invalid identifier")
    return value


def _portable_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 180 or "\\" in value or ":" in value or value.startswith("/"):
        raise ThemeError("theme: unsafe asset path")
    parts = value.split("/")
    if (len(parts) != 2 or parts[0] != "assets" or
            any(not part or part in {".", ".."} or part.endswith((".", " ")) or
                any(char in '<>"|?*' for char in part) or
                any(ord(char) < 32 for char in part) or
                part.split(".")[0].upper() in _RESERVED for part in parts)):
        raise ThemeError("theme: unsafe asset path")
    if unicodedata.normalize("NFC", value) != value:
        raise ThemeError("theme: nonportable asset path")
    if not value.endswith(".wav"):
        raise ThemeError("theme: unsupported asset type")
    return value


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ThemeError("theme: missing or unreadable file") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ThemeError("theme: unsafe or oversized file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                    (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or
                    opened.st_size > limit):
                raise ThemeError("theme: file changed during validation")
            raw = os.read(descriptor, limit + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ThemeError("theme: unreadable file") from exc
    if len(raw) > limit or len(raw) != opened.st_size:
        raise ThemeError("theme: oversized or changing file")
    return raw


@dataclass(frozen=True)
class Theme:
    theme_id: str
    version: str
    author: str
    license: str
    cues: dict[str, dict]
    assets: dict[str, dict]
    capabilities: frozenset[str]

    def resolve(self, event: CueEvent, available: frozenset[str], rgb_level_cap: float = 1.0) -> dict:
        """Return logical plans only. Silence is an explicit safe fallback."""
        if type(rgb_level_cap) not in (float, int) or not 0 <= rgb_level_cap <= 1:
            raise ValueError("RGB level cap must be within 0..1")
        cue = self.cues.get(event.cue_id, {})
        result = {}
        for channel in ("text", "rgb", "audio"):
            if channel not in self.capabilities or channel not in available or channel not in cue:
                result[channel] = {"outcome": "silent"}
                continue
            selection = cue[channel]
            if channel == "text":
                result[channel] = ({"outcome": "silent"} if event.text is None else
                                   {"outcome": "planned", "content": html.escape(selection["prefix"] + event.text)})
            elif channel == "rgb":
                result[channel] = {"outcome": "planned", "zones": list(selection["zones"]),
                                   "color": list(selection["color"]),
                                   "level": min(selection["level"], rgb_level_cap)}
            else:
                asset = self.assets[selection["asset"]]
                result[channel] = {"outcome": "planned", "asset": selection["asset"],
                                   "sha256": asset["sha256"]}
        return result


def load_directory(root: Path) -> Theme:
    """Validate and preview a local pack. Archive import/media decoding are separate gates."""
    root = Path(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink():
        raise ThemeError("theme: root must be a directory")
    manifest_path = root / "manifest.json"
    raw = _read_regular(manifest_path, MAX_MANIFEST)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ThemeError("theme: duplicate JSON key")
            result[key] = value
        return result
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ThemeError("theme: invalid JSON") from exc
    _fields(data, {"schema_version", "id", "version", "author", "license", "provenance",
                   "capabilities", "assets", "cues"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ThemeError("theme: unsupported version")
    theme_id = _name(data["id"])
    version = data["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ThemeError("theme: invalid version")
    for key in ("author", "provenance"):
        if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 160:
            raise ThemeError("theme: missing provenance")
    license_id = data["license"]
    if not isinstance(license_id, str) or not _LICENSE.fullmatch(license_id):
        raise ThemeError("theme: invalid license")
    caps = data["capabilities"]
    if (not isinstance(caps, list) or any(not isinstance(cap, str) for cap in caps) or
            len(caps) != len(set(caps)) or any(cap not in _CHANNELS for cap in caps)):
        raise ThemeError("theme: invalid capabilities")
    assets = data["assets"]
    cues = data["cues"]
    if not isinstance(assets, dict) or len(assets) > MAX_FILES or not isinstance(cues, dict) or len(cues) > 64:
        raise ThemeError("theme: too many entries")
    checked_assets = {}
    names_seen = set()
    total = len(raw)
    for asset_name, spec in assets.items():
        path = _portable_path(asset_name)
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in names_seen:
            raise ThemeError("theme: colliding asset paths")
        names_seen.add(folded)
        _fields(spec, {"sha256", "license", "provenance"})
        if (not isinstance(spec["sha256"], str) or not _DIGEST.fullmatch(spec["sha256"]) or
                not isinstance(spec["license"], str) or not _LICENSE.fullmatch(spec["license"]) or
                not isinstance(spec["provenance"], str) or not 1 <= len(spec["provenance"]) <= 160):
            raise ThemeError("theme: invalid asset provenance")
        target = root / path
        if target.parent.is_symlink():
            raise ThemeError("theme: symlink directory")
        content = _read_regular(target, MAX_ASSET)
        total += len(content)
        if total > MAX_TOTAL or hashlib.sha256(content).hexdigest() != spec["sha256"]:
            raise ThemeError("theme: asset size or digest mismatch")
        checked_assets[path] = dict(spec)
    checked_cues = {}
    for cue_name, mapping in cues.items():
        _name(cue_name)
        if not isinstance(mapping, dict) or mapping.keys() - _CHANNELS:
            raise ThemeError("theme: invalid cue mapping")
        checked = {}
        if "text" in mapping:
            text_spec = _fields(mapping["text"], {"prefix"})
            prefix = text_spec["prefix"]
            if (not isinstance(prefix, str) or len(prefix) > 32 or
                    any(ord(char) < 32 for char in prefix)):
                raise ThemeError("theme: invalid text prefix")
            checked["text"] = {"prefix": prefix}
        if "rgb" in mapping:
            rgb = _fields(mapping["rgb"], {"zones", "color", "level"})
            zones, color, level = rgb["zones"], rgb["color"], rgb["level"]
            if (not isinstance(zones, list) or not 1 <= len(zones) <= 8 or
                    any(not isinstance(zone, str) or not _NAME.fullmatch(zone) for zone in zones) or
                    len(set(zones)) != len(zones) or not isinstance(color, list) or len(color) != 3 or
                    any(type(component) is not int or not 0 <= component <= 255 for component in color) or
                    type(level) not in (int, float) or not 0 <= level <= 1):
                raise ThemeError("theme: invalid RGB mapping")
            checked["rgb"] = {"zones": list(zones), "color": list(color), "level": level}
        if "audio" in mapping:
            audio = _fields(mapping["audio"], {"asset"})
            if not isinstance(audio["asset"], str) or audio["asset"] not in checked_assets:
                raise ThemeError("theme: missing audio asset")
            checked["audio"] = {"asset": audio["asset"]}
        if any(channel not in caps for channel in checked):
            raise ThemeError("theme: cue exceeds declared capability")
        checked_cues[cue_name] = checked
    listed = {"manifest.json", *checked_assets}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ThemeError("theme: symlink is forbidden")
        if path.is_dir():
            if relative != "assets":
                raise ThemeError("theme: unexpected directory")
        elif relative not in listed:
            raise ThemeError("theme: unexpected file")
    if len(listed) > MAX_FILES:
        raise ThemeError("theme: too many files")
    return Theme(theme_id, version, data["author"], license_id,
                 checked_cues, checked_assets, frozenset(caps))
