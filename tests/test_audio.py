"""Synthetic, fake-only tests for the explicit-device bounded audio contract."""

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from starforge_cues import Coordinator, CueEvent, FakeSink, QuietPolicy
from starforge_cues.audio import (AudioAdapter, AudioAssetError, AudioClip,
                                  DEFAULT_GAIN, FakeAudioBackend, MAX_GAIN, load_audio_clips,
                                  original_test_earcon)
from starforge_cues.themes import ThemeError

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()
SELECTED = "sink.usb.selected"
OTHER = "sink.hdmi.other"


def event(at: float, **changes):
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return CueEvent.from_mapping({**FIXTURE, "occurred_at": stamp, "observed_at": stamp, **changes})


def wav_with_seconds(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * int(24000 * seconds))
    return buffer.getvalue()


class AudioTests(unittest.TestCase):
    def setup_audio(self, clock=None, sinks=None):
        clock = clock or [NOW]
        backend = FakeAudioBackend(frozenset(sinks if sinks is not None else {SELECTED, OTHER}),
                                   monotonic_clock=lambda: clock[0])
        clip = original_test_earcon()
        adapter = AudioAdapter(backend, SELECTED, {"job.completed": clip, "urgent": clip},
                               gain=0.05, monotonic_clock=lambda: clock[0])
        core = Coordinator({"audio": adapter, "rgb": FakeSink()}, clock=lambda: clock[0])
        return core, backend, adapter, clock

    def test_original_clip_is_bounded_and_corrupt_assets_rejected(self):
        clip = original_test_earcon()
        self.assertEqual(clip.duration_ms, 500)
        self.assertEqual(clip.channels, 1)
        self.assertLess(len(clip.wav), 1024 * 1024)
        with wave.open(io.BytesIO(clip.wav), "rb") as reader:
            samples = reader.readframes(reader.getnframes())
        self.assertEqual(struct.unpack_from("<h", samples, 0)[0], 0)
        self.assertEqual(struct.unpack_from("<h", samples, 12000)[0], 0)  # note boundary
        with self.assertRaises(AudioAssetError):
            AudioClip.from_wav(clip.wav, "0" * 64)
        with self.assertRaises(AudioAssetError):
            AudioClip.from_wav(clip.wav[:-20], hashlib.sha256(clip.wav[:-20]).hexdigest())
        long = wav_with_seconds(0.6)
        with self.assertRaises(AudioAssetError):
            AudioClip.from_wav(long, hashlib.sha256(long).hexdigest())
        forged = AudioClip(long, hashlib.sha256(long).hexdigest(), 100, 1, 24000)
        with self.assertRaises(AudioAssetError):
            AudioAdapter(FakeAudioBackend(frozenset({SELECTED})), SELECTED,
                         {"job.completed": forged})
        with self.assertRaises(ValueError):
            AudioAdapter(FakeAudioBackend(frozenset({SELECTED})), SELECTED,
                         {"job.completed": clip}, gain=MAX_GAIN + 0.001)

    def test_commissioning_gain_requires_explicit_override_and_hard_cap(self):
        clip = original_test_earcon()
        backend = FakeAudioBackend(frozenset({SELECTED}))
        default = AudioAdapter(backend, SELECTED, {"job.completed": clip})
        self.assertEqual(default.gain, DEFAULT_GAIN)
        for gain in (0.15, MAX_GAIN + 0.001, float("nan")):
            with self.assertRaises(ValueError):
                AudioAdapter(backend, SELECTED, {"job.completed": clip}, gain=gain)
        with self.assertRaises(ValueError):
            AudioAdapter(backend, SELECTED, {"job.completed": clip}, gain=0.15,
                         commissioning_override="true")
        with self.assertRaises(ValueError):
            AudioAdapter(backend, SELECTED, {"job.completed": clip}, gain=MAX_GAIN + 0.001,
                         commissioning_override=True)
        selected = AudioAdapter(backend, SELECTED, {"job.completed": clip}, gain=0.15,
                                commissioning_override=True)
        self.assertEqual(selected.dispatch("audio", {"operation": "cue",
                                                      "cue_id": "job.completed"}), "accepted")
        self.assertEqual(backend.starts[-1][2], 0.15)

    def test_data_only_theme_asset_is_revalidated_before_use(self):
        clip = original_test_earcon()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            asset = root / "assets" / "earcon.wav"
            asset.write_bytes(clip.wav)
            manifest = {"schema_version": 1, "id": "synthetic-audio", "version": "0.1.0",
                        "author": "Starforge contributors", "license": "MIT",
                        "provenance": "Original generated earcon for this project",
                        "capabilities": ["audio"],
                        "assets": {"assets/earcon.wav": {"sha256": clip.sha256,
                                                         "license": "MIT",
                                                         "provenance": "Original generated PCM"}},
                        "cues": {"job.completed": {"audio": {"asset": "assets/earcon.wav"}}}}
            (root / "manifest.json").write_text(json.dumps(manifest))
            loaded = load_audio_clips(root)
            self.assertEqual(loaded["job.completed"].sha256, clip.sha256)
            asset.write_bytes(clip.wav + b"changed")
            with self.assertRaises(ThemeError):
                load_audio_clips(root)

    def test_selected_sink_only_no_fallback_and_independent_rgb(self):
        core, backend, adapter, clock = self.setup_audio(sinks={OTHER})
        result = core.handle(event(clock[0]))
        self.assertEqual(result["channels"]["audio"], "unsupported")
        self.assertEqual(result["channels"]["rgb"], "accepted")
        self.assertEqual(backend.starts, [])
        backend.sinks = frozenset({SELECTED, OTHER})
        result = core.handle(event(clock[0], event_id="next", idempotency_key="next",
                                   subject_id="next"))
        self.assertEqual(result["channels"]["audio"], "accepted")
        self.assertEqual(backend.starts[0][0], SELECTED)
        self.assertEqual(backend.starts[0][2], 0.05)
        self.assertEqual(adapter.selected_sink, SELECTED)

    def test_quiet_mute_cancel_and_restore_never_replay_old_sound(self):
        core, backend, _adapter, clock = self.setup_audio()
        core.set_policy(QuietPolicy(quiet=True))
        self.assertEqual(core.handle(event(clock[0]))["channels"]["audio"], "suppressed")
        self.assertEqual(backend.starts, [])
        core.set_policy(QuietPolicy())
        self.assertEqual(backend.starts, [])
        core.handle(event(clock[0], event_id="low", idempotency_key="low",
                          subject_id="low"))
        self.assertEqual(len(backend.starts), 1)
        core.handle(event(clock[0], event_id="high", idempotency_key="high",
                          subject_id="high", cue_id="urgent", severity="critical"))
        self.assertEqual(len(backend.starts), 2)
        self.assertEqual(len(backend.stops), 1)
        self.assertEqual(len(backend.active), 1)  # one voice, no overlap
        self.assertEqual(backend.stops[0][2], 50)  # bounded fade request
        core.handle(event(clock[0], event_id="cancel", idempotency_key="cancel",
                          subject_id="high", status="cancelled"))
        self.assertEqual(len(backend.starts), 2)  # lower-priority restore is silent
        self.assertEqual(len(backend.stops), 2)
        core.set_policy(QuietPolicy(muted=frozenset({"audio"})))
        core.set_policy(QuietPolicy())
        self.assertEqual(len(backend.starts), 2)

    def test_expiry_and_restart_clear_without_old_replay(self):
        core, backend, _adapter, clock = self.setup_audio()
        core.handle(event(clock[0], ttl_ms=1000))
        self.assertEqual(len(backend.starts), 1)
        clock[0] += 2
        self.assertEqual(core.tick()["channels"]["audio"], "accepted")
        self.assertEqual(len(backend.stops), 1)
        self.assertEqual(backend.active, set())
        new_backend = FakeAudioBackend(frozenset({SELECTED}))
        adapter = AudioAdapter(new_backend, SELECTED, {"job.completed": original_test_earcon()})
        recovered = Coordinator.recover_baseline(1, None, sinks={"audio": adapter},
                                                 clock=lambda: clock[0])
        self.assertEqual(recovered.recovery_result["previous_output"], "unknown")
        self.assertEqual(new_backend.starts, [])

    def test_ambiguous_or_failed_start_never_retries_automatically(self):
        core, backend, _adapter, clock = self.setup_audio()
        backend.unknown_start = True
        self.assertEqual(core.handle(event(clock[0]))["channels"]["audio"], "unknown")
        self.assertEqual(len(backend.starts), 1)
        core.tick()
        self.assertEqual(len(backend.starts), 1)
        core.handle(event(clock[0], event_id="second", idempotency_key="second",
                          subject_id="second"))
        self.assertEqual(len(backend.starts), 1)
        clock[0] += 0.6
        backend.unknown_start = False
        core.handle(event(clock[0], event_id="third", idempotency_key="third",
                          subject_id="third"))
        self.assertEqual(len(backend.starts), 2)
        core, backend, _adapter, clock = self.setup_audio()
        backend.fail_start = True
        result = core.handle(event(clock[0]))
        self.assertEqual(result["channels"]["audio"], "unknown")
        self.assertEqual(result["channels"]["rgb"], "accepted")
        core.tick()
        self.assertEqual(len(backend.starts), 1)

    def test_lost_ack_after_start_blocks_second_voice_same_clock(self):
        core, backend, _adapter, clock = self.setup_audio()
        backend.raise_after_start = True
        first = core.handle(event(clock[0], event_id="first", idempotency_key="first",
                                  subject_id="first"))
        self.assertEqual(first["channels"]["audio"], "unknown")
        self.assertEqual(len(backend.active), 1)  # fake voice may already be playing
        second = core.handle(event(clock[0], event_id="second", idempotency_key="second",
                                   subject_id="second"))
        self.assertEqual(second["channels"]["audio"], "unknown")
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(len(backend.active), 1)
        core.tick()
        self.assertEqual(len(backend.starts), 1)
        clock[0] += 0.6
        self.assertEqual(backend.active, set())  # bounded one-shot ended without a token
        backend.raise_after_start = False
        core.handle(event(clock[0], event_id="third", idempotency_key="third",
                          subject_id="third"))
        self.assertEqual(len(backend.starts), 2)
        self.assertEqual(len(backend.active), 1)

    def test_unmapped_replacement_stops_old_voice_and_reports_stop_failure(self):
        core, backend, _adapter, clock = self.setup_audio()
        core.handle(event(clock[0], event_id="mapped", idempotency_key="mapped",
                          subject_id="mapped"))
        self.assertEqual(len(backend.active), 1)
        replacement = core.handle(event(clock[0], event_id="unmapped", idempotency_key="unmapped",
                                        subject_id="unmapped", cue_id="not.mapped"))
        self.assertEqual(replacement["channels"]["audio"], "unsupported")
        self.assertEqual(replacement["channels"]["rgb"], "accepted")
        self.assertEqual(len(backend.stops), 1)
        self.assertEqual(backend.active, set())
        self.assertEqual(len(backend.starts), 1)

        core, backend, _adapter, clock = self.setup_audio()
        core.handle(event(clock[0], event_id="mapped", idempotency_key="mapped",
                          subject_id="mapped"))
        backend.fail_stop = True
        failed = core.handle(event(clock[0], event_id="unmapped", idempotency_key="unmapped",
                                   subject_id="unmapped", cue_id="not.mapped"))
        self.assertEqual(failed["channels"]["audio"], "failed")
        self.assertEqual(failed["channels"]["rgb"], "accepted")
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(len(backend.active), 1)

    def test_missing_cue_stale_event_and_selected_sink_loss(self):
        core, backend, _adapter, clock = self.setup_audio()
        result = core.handle(event(clock[0], cue_id="unmapped"))
        self.assertEqual(result["channels"]["audio"], "unsupported")
        self.assertEqual(backend.starts, [])
        stale = event(clock[0] - 400, event_id="stale", idempotency_key="stale",
                      subject_id="stale")
        self.assertEqual(core.handle(stale)["reason"], "stale")
        self.assertEqual(backend.starts, [])
        core.handle(event(clock[0], event_id="live", idempotency_key="live",
                          subject_id="live"))
        backend.sinks = frozenset({OTHER})
        result = core.handle(event(clock[0], event_id="lost", idempotency_key="lost",
                                   subject_id="lost"))
        self.assertEqual(result["channels"]["audio"], "unsupported")
        self.assertEqual(len(backend.starts), 1)

    def test_failed_clear_is_retryable_but_not_other_channels(self):
        core, backend, _adapter, clock = self.setup_audio()
        core.handle(event(clock[0]))
        backend.fail_stop = True
        result = core.set_policy(QuietPolicy(muted=frozenset({"audio"})))
        self.assertEqual(result["channels"]["audio"], "failed")
        backend.fail_stop = False
        self.assertEqual(core.tick()["channels"], {"audio": "accepted"})
        self.assertEqual(len(backend.starts), 1)


if __name__ == "__main__":
    unittest.main()
