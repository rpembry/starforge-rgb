"""Bounded audio sink contract with a fake backend; no playback engine is invoked.

The host chooses one exact output sink and validated local PCM WAV clips. The
portable coordinator remains the policy owner. Backends must never substitute
another output or automatically repeat an ambiguous start.
"""

from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
import re
import struct
from threading import RLock
from typing import Callable, Protocol
import wave

from .themes import _read_regular, load_directory

MAX_WAV_BYTES = 1024 * 1024
MAX_CLIPS = 16
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DURATION_MS = 500
MAX_GAIN = 0.05
FADE_MS = 50
_SINK_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}\Z")
_CUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class AudioAssetError(ValueError):
    pass


@dataclass(frozen=True)
class AudioClip:
    """A validated bounded PCM WAV asset; raw bytes are never logged."""

    wav: bytes
    sha256: str
    duration_ms: float
    channels: int
    sample_rate: int

    @classmethod
    def from_wav(cls, raw: bytes, expected_sha256: str) -> "AudioClip":
        if (not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_WAV_BYTES or
                not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256) or
                hashlib.sha256(raw).hexdigest() != expected_sha256):
            raise AudioAssetError("audio asset size or digest invalid")
        try:
            with wave.open(io.BytesIO(raw), "rb") as reader:
                channels = reader.getnchannels()
                rate = reader.getframerate()
                frames = reader.getnframes()
                if (reader.getcomptype() != "NONE" or reader.getsampwidth() != 2 or
                        channels not in (1, 2) or not 8000 <= rate <= 48000 or
                        not 1 <= frames <= rate * MAX_DURATION_MS // 1000):
                    raise AudioAssetError("audio asset format or duration unsupported")
                samples = reader.readframes(frames)
        except (wave.Error, EOFError, ValueError) as exc:
            raise AudioAssetError("audio asset is not bounded PCM WAV") from exc
        if len(samples) != frames * channels * 2:
            raise AudioAssetError("audio asset frames truncated")
        return cls(raw, expected_sha256, frames * 1000 / rate, channels, rate)


def original_test_earcon() -> AudioClip:
    """Generate an original 0.5-second, two-note test clip in memory only."""
    rate = 24000
    frames = rate // 2
    samples = bytearray()
    for index in range(frames):
        seconds = index / rate
        frequency = 523.25 if seconds < 0.25 else 659.25
        note_time = seconds if seconds < 0.25 else seconds - 0.25
        fade = min(1.0, note_time / 0.02, (0.25 - note_time) / 0.02)
        value = int(32767 * 0.18 * max(0.0, fade) *
                    math.sin(2 * math.pi * frequency * note_time))
        samples.extend(struct.pack("<h", value))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples)
    raw = buffer.getvalue()
    return AudioClip.from_wav(raw, hashlib.sha256(raw).hexdigest())


def load_audio_clips(root: Path) -> dict[str, AudioClip]:
    """Load only cue-mapped assets from a freshly validated data-only theme."""
    theme = load_directory(root)
    audio_cues = {cue: mapping["audio"]["asset"] for cue, mapping in theme.cues.items()
                  if "audio" in mapping}
    if len(audio_cues) > MAX_CLIPS:
        raise AudioAssetError("too many audio cue mappings")
    clips = {}
    for cue, asset_name in audio_cues.items():
        raw = _read_regular(Path(root) / asset_name, MAX_WAV_BYTES)
        clips[cue] = AudioClip.from_wav(raw, theme.assets[asset_name]["sha256"])
    return clips


@dataclass(frozen=True)
class PlaybackReceipt:
    outcome: str  # accepted or unknown; unknown may already have sounded
    token: str | None


class AudioBackend(Protocol):
    def available_sinks(self) -> frozenset[str]: ...
    def start(self, sink_id: str, wav: bytes, gain: float) -> PlaybackReceipt: ...
    def stop(self, sink_id: str, token: str, fade_ms: int) -> str: ...


class AudioAdapter:
    """Exact-device one-voice sink; no queue, default fallback, or system volume change."""

    capabilities = frozenset({"audio"})

    def __init__(self, backend: AudioBackend, selected_sink: str,
                 clips: dict[str, AudioClip], *, gain: float = MAX_GAIN,
                 monotonic_clock: Callable[[], float] | None = None):
        import time
        if not isinstance(selected_sink, str) or not _SINK_ID.fullmatch(selected_sink):
            raise ValueError("invalid selected audio sink")
        if (type(gain) not in (int, float) or not 0 <= gain <= MAX_GAIN or
                not isinstance(clips, dict) or len(clips) > MAX_CLIPS or
                any(not isinstance(cue, str) or not _CUE_ID.fullmatch(cue) or
                    not isinstance(clip, AudioClip) for cue, clip in clips.items()) or
                sum(len(clip.wav) for clip in clips.values()) > MAX_TOTAL_BYTES):
            raise ValueError("invalid audio adapter bounds")
        checked_clips = {cue: AudioClip.from_wav(clip.wav, clip.sha256)
                         for cue, clip in clips.items()}
        self.backend = backend
        self.selected_sink = selected_sink
        self.clips = checked_clips
        self.gain = gain
        self.monotonic_clock = monotonic_clock or time.monotonic
        self._lock = RLock()
        self._active_token: str | None = None
        self._uncertain_until = 0.0

    def _stop(self) -> str:
        if self._active_token is None:
            # Real backends own any process even when its start receipt was
            # lost. They must establish quiescence; elapsed clip time cannot.
            quiesce = getattr(self.backend, "quiesce_unknown", None)
            if quiesce is not None:
                try:
                    return "accepted" if quiesce() == "accepted" else "unknown"
                except Exception:
                    return "unknown"
            return "unknown" if self.monotonic_clock() < self._uncertain_until else "accepted"
        outcome = self.backend.stop(self.selected_sink, self._active_token, FADE_MS)
        if outcome == "accepted":
            self._active_token = None
        return outcome if outcome in ("accepted", "unknown") else "unknown"

    def dispatch(self, channel: str, plan: dict) -> str:
        if channel != "audio":
            return "unsupported"
        with self._lock:
            operation = plan["operation"]
            if operation in ("clear", "restore"):
                # Restoration of an older lease/baseline must never replay sound.
                return self._stop()
            if operation != "cue":
                return "unsupported"
            # A replacement makes the previous sound obsolete even when the
            # replacement cue is unmapped or its selected sink disappeared.
            stopped = self._stop()
            if stopped != "accepted":
                return stopped
            clip = self.clips.get(plan["cue_id"])
            if clip is None or self.selected_sink not in self.backend.available_sinks():
                return "unsupported"
            try:
                receipt = self.backend.start(self.selected_sink, clip.wav, self.gain)
            except Exception:
                # The backend may have started playback before its receipt was
                # lost. With no token to stop, wait through the bounded clip.
                self._uncertain_until = self.monotonic_clock() + clip.duration_ms / 1000
                return "unknown"
            if not isinstance(receipt, PlaybackReceipt) or receipt.outcome not in ("accepted", "unknown"):
                self._uncertain_until = self.monotonic_clock() + clip.duration_ms / 1000
                return "unknown"
            if receipt.token is not None and (not isinstance(receipt.token, str) or
                                               not 1 <= len(receipt.token) <= 128):
                self._uncertain_until = self.monotonic_clock() + clip.duration_ms / 1000
                return "unknown"
            if receipt.outcome == "accepted" and not receipt.token:
                self._uncertain_until = self.monotonic_clock() + clip.duration_ms / 1000
                return "unknown"
            self._active_token = receipt.token
            if receipt.outcome == "unknown" and receipt.token is None:
                self._uncertain_until = self.monotonic_clock() + clip.duration_ms / 1000
            return receipt.outcome


class FakeAudioBackend:
    """Records fake starts/stops; never loads a platform audio library."""

    def __init__(self, sinks: frozenset[str],
                 monotonic_clock: Callable[[], float] | None = None):
        import time
        self.sinks = sinks
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.starts: list[tuple[str, str, float]] = []
        self.stops: list[tuple[str, str, int]] = []
        self._active_until: dict[str, float] = {}
        self.fail_start = False
        self.fail_stop = False
        self.unknown_start = False
        self.unknown_stop = False
        self.raise_after_start = False

    def available_sinks(self) -> frozenset[str]:
        return self.sinks

    @property
    def active(self) -> set[str]:
        now = self.monotonic_clock()
        self._active_until = {token: end for token, end in self._active_until.items()
                              if end > now}
        return set(self._active_until)

    def start(self, sink_id: str, wav: bytes, gain: float) -> PlaybackReceipt:
        if sink_id not in self.sinks:
            raise RuntimeError("selected sink unavailable")
        self.starts.append((sink_id, hashlib.sha256(wav).hexdigest(), gain))
        if self.fail_start:
            raise RuntimeError("fake start failure")
        if self.unknown_start:
            return PlaybackReceipt("unknown", None)
        token = f"fake-{len(self.starts)}"
        with wave.open(io.BytesIO(wav), "rb") as reader:
            duration_s = reader.getnframes() / reader.getframerate()
        self._active_until[token] = self.monotonic_clock() + duration_s
        if self.raise_after_start:
            raise RuntimeError("fake lost start acknowledgment")
        return PlaybackReceipt("accepted", token)

    def stop(self, sink_id: str, token: str, fade_ms: int) -> str:
        self.stops.append((sink_id, token, fade_ms))
        if self.fail_stop:
            raise RuntimeError("fake stop failure")
        if self.unknown_stop:
            return "unknown"
        self._active_until.pop(token, None)
        return "accepted"
