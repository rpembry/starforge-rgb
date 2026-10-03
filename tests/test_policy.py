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
        self.assertEqual(core.handle(event())["result"], "accepted")
        second = event(event_id="second", idempotency_key="second")
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


if __name__ == "__main__":
    unittest.main()
