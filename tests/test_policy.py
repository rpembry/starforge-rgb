"""Deterministic policy checks; no platform outputs."""

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from starforge_cues import Coordinator, CueEvent, FakeSink, QuietPolicy, QuietWindow

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()


def event(**changes):
    return CueEvent.from_mapping({**FIXTURE, **changes})


class PolicyFoundationTests(unittest.TestCase):
    def test_current_baseline_generation_wins_after_expiry(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0])
        core.set_baseline(1, "ambient.first")
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.first")
        core.handle(event())
        core.set_baseline(2, "ambient.current")
        self.assertEqual(rgb.state["rgb"]["cue_id"], "job.completed")
        with self.assertRaises(ValueError):
            core.set_baseline(1, "ambient.old")
        clock[0] += 241
        self.assertEqual(core.tick()["plan"]["baseline_generation"], 2)
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.current")

    def test_mute_and_unmute_clear_then_restore_without_audio_replay(self):
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: NOW)
        core.handle(event())
        audio_count = len(sinks["audio"].calls)
        core.set_policy(QuietPolicy(muted=frozenset({"text", "rgb", "audio"})))
        for channel in sinks:
            self.assertIsNone(sinks[channel].state[channel])
        core.set_policy(QuietPolicy())
        self.assertEqual(sinks["text"].state["text"]["text"], "Synthetic job completed.")
        self.assertEqual(sinks["rgb"].state["rgb"]["cue_id"], "job.completed")
        self.assertEqual(len(sinks["audio"].calls), audio_count + 1)  # clear only

    def test_midnight_quiet_window_and_dnd(self):
        window = QuietWindow(23 * 60, 7 * 60)
        policy = QuietPolicy(windows=(window,), local_timezone=timezone.utc)
        def at(hour, minute):
            return datetime(2026, 10, 3, hour, minute, tzinfo=timezone.utc).timestamp()
        self.assertFalse(policy.permits("rgb", at(23, 0)))
        self.assertFalse(policy.permits("audio", at(6, 59)))
        self.assertTrue(policy.permits("text", at(7, 0)))
        self.assertTrue(policy.permits("rgb", at(22, 59)))
        self.assertFalse(QuietPolicy(dnd=True).permits("text", NOW))
        with self.assertRaises(ValueError):
            QuietWindow(1440, 0)

    def test_cooldown_and_self_origin(self):
        core = Coordinator(clock=lambda: NOW, cooldown_seconds=10,
                           own_origins=frozenset({"starforge-host"}))
        self.assertEqual(core.handle(event(origin_id="starforge-host"))["reason"], "self_origin")
        self.assertEqual(core.handle(event(status="started"))["result"], "accepted")
        second = event(event_id="second", idempotency_key="second", status="progress")
        self.assertEqual(core.handle(second)["reason"], "cooldown")

    def test_unmute_after_expiry_never_restores_stale_cue(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0])
        core.handle(event())
        core.set_policy(QuietPolicy(muted=frozenset({"rgb"})))
        clock[0] += 241
        core.set_policy(QuietPolicy())
        self.assertIsNone(rgb.state["rgb"])
        self.assertNotEqual(rgb.calls[-1][1].get("cue_id"), "job.completed")

    def test_failed_mute_clear_retries_after_recovery(self):
        for policy in (QuietPolicy(muted=frozenset({"rgb"})), QuietPolicy(dnd=True),
                       QuietPolicy(quiet=True)):
            with self.subTest(policy=policy):
                rgb = FakeSink()
                core = Coordinator({"rgb": rgb}, clock=lambda: NOW)
                core.handle(event())
                rgb.fail = frozenset({"rgb"})
                self.assertEqual(core.set_policy(policy)["channels"]["rgb"], "failed")
                self.assertIsNotNone(rgb.state["rgb"])
                rgb.fail = frozenset()
                self.assertEqual(core.tick()["channels"], {"rgb": "accepted"})
                self.assertIsNone(rgb.state["rgb"])

    def test_unmute_after_short_critical_expiry_does_not_replay_low_audio(self):
        clock = [NOW]
        audio = FakeSink()
        core = Coordinator({"audio": audio}, clock=lambda: clock[0])
        core.handle(event(event_id="low", idempotency_key="low", subject_id="low"))
        core.handle(event(event_id="high", idempotency_key="high", subject_id="high",
                          severity="critical", ttl_ms=1000))
        core.set_policy(QuietPolicy(muted=frozenset({"audio"})))
        count = len(audio.calls)
        clock[0] += 2
        core.set_policy(QuietPolicy())  # no timer tick between expiry and unmute
        self.assertEqual(len(audio.calls), count)
        self.assertEqual(core.tick()["result"], "unchanged")
        self.assertEqual(len(audio.calls), count)

    def test_quiet_window_failed_clear_retries_on_tick(self):
        start = datetime(2026, 10, 3, 22, 59, tzinfo=timezone.utc)
        clock = [start.timestamp()]
        rgb = FakeSink()
        policy = QuietPolicy(windows=(QuietWindow(23 * 60, 7 * 60),))
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0], policy=policy)
        core.handle(event(event_id="late", idempotency_key="late",
                          occurred_at="2026-10-03T22:59:00Z",
                          observed_at="2026-10-03T22:59:00Z"))
        rgb.fail = frozenset({"rgb"})
        clock[0] += 60
        self.assertEqual(core.tick()["channels"]["rgb"], "failed")
        rgb.fail = frozenset()
        self.assertEqual(core.tick()["channels"], {"rgb": "accepted"})
        self.assertIsNone(rgb.state["rgb"])


if __name__ == "__main__":
    unittest.main()
