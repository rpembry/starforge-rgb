"""Lifecycle policy tests with synthetic events, fake clock and fake sinks."""

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starforge_cues import Coordinator, CueEvent, FakeSink, QuietPolicy, QuietWindow

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()


def event(at: float, **changes):
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return CueEvent.from_mapping({**FIXTURE, "occurred_at": stamp, "observed_at": stamp, **changes})


class LifecycleTests(unittest.TestCase):
    def test_renewal_horizon_requires_new_subject_after_cap(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0], max_lease_age_s=300)
        core.set_baseline(1, "ambient")
        core.handle(event(clock[0], event_id="first", idempotency_key="first", subject_id="run-one"))
        clock[0] += 200
        renewed = core.handle(event(clock[0], event_id="renew", idempotency_key="renew",
                                    subject_id="run-one", status="progress"))
        self.assertEqual(renewed["plan"]["expires_at"],
                         datetime.fromtimestamp(NOW + 300, timezone.utc).isoformat())
        clock[0] += 101
        core.tick()
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient")
        blocked = core.handle(event(clock[0], event_id="again", idempotency_key="again",
                                    subject_id="run-one", status="progress"))
        self.assertEqual(blocked["reason"], "lease_limit")
        fresh = core.handle(event(clock[0], event_id="new-run", idempotency_key="new-run",
                                  subject_id="run-two", status="started"))
        self.assertEqual(fresh["result"], "accepted")
        self.assertLessEqual(len(core._leases) + len(core._retired_subjects), 4096)

    def test_horizon_restores_lower_priority_then_current_baseline(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0], max_lease_age_s=300)
        core.set_baseline(1, "ambient")
        core.handle(event(clock[0], event_id="high", idempotency_key="high",
                          subject_id="high", severity="critical", cue_id="urgent"))
        clock[0] += 100
        core.handle(event(clock[0], event_id="low", idempotency_key="low",
                          subject_id="low", severity="info", cue_id="progress"))
        clock[0] += 100
        core.handle(event(clock[0], event_id="renew-high", idempotency_key="renew-high",
                          subject_id="high", severity="critical", cue_id="urgent", status="progress"))
        clock[0] += 101
        core.tick()
        self.assertEqual(rgb.state["rgb"]["cue_id"], "progress")
        clock[0] += 100
        core.tick()
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient")

    def test_restart_reports_unknown_prior_state_and_current_baseline_dispatch(self):
        rgb = FakeSink()
        audio = FakeSink()
        old = Coordinator({"rgb": rgb, "audio": audio}, clock=lambda: NOW)
        old.handle(event(NOW))
        audio_calls = len(audio.calls)
        fresh = Coordinator.recover_baseline(2, "ambient.current", sinks={"rgb": rgb, "audio": audio},
                                             clock=lambda: NOW)
        self.assertEqual(fresh.recovery_result["previous_output"], "unknown")
        self.assertEqual(fresh.recovery_result["baseline_dispatch"]["channels"]["rgb"], "accepted")
        self.assertEqual(fresh.recovery_result["audio_clear"], "accepted")
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.current")
        self.assertIsNone(audio.state["audio"])
        self.assertEqual(len(fresh._leases), 0)
        self.assertEqual(len(audio.calls), audio_calls + 1)  # clear only, no old sound replay
        self.assertEqual(audio.calls[-1][1]["operation"], "clear")
        rgb.fail = frozenset({"rgb"})
        failed = Coordinator.recover_baseline(1, None, sinks={"rgb": rgb}, clock=lambda: NOW)
        self.assertEqual(failed.recovery_result["baseline_dispatch"]["channels"]["rgb"], "failed")
        self.assertEqual(failed.recovery_result["previous_output"], "unknown")

    def test_feedback_filter_is_exact_and_expires(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        core.record_feedback("desktop.bridge", "flow-one", ttl_s=10)
        echo = event(clock[0], event_id="echo", idempotency_key="echo",
                     source_id="desktop.bridge", correlation_id="flow-one")
        self.assertEqual(core.handle(echo)["reason"], "feedback_loop")
        unrelated = event(clock[0], event_id="other", idempotency_key="other",
                          source_id="other.source", correlation_id="flow-one")
        self.assertEqual(core.handle(unrelated)["result"], "accepted")
        clock[0] += 11
        self.assertEqual(core.handle(event(clock[0], event_id="later", idempotency_key="later",
                                           source_id="desktop.bridge", correlation_id="flow-one"))["result"],
                         "accepted")
        with self.assertRaises(ValueError):
            core.record_feedback("desktop.bridge", "flow-one", ttl_s=301)

    def test_dst_fold_gap_and_backward_clock_jump(self):
        try:
            zone = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("IANA time zone database unavailable")
        policy = QuietPolicy(windows=(QuietWindow(60, 120),), local_timezone=zone)
        def utc(month, day, hour, minute):
            return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
        # Both 01:30 occurrences in the November fall-back hour are quiet.
        self.assertFalse(policy.permits("rgb", utc(11, 1, 5, 30)))
        self.assertFalse(policy.permits("rgb", utc(11, 1, 6, 30)))
        self.assertTrue(policy.permits("rgb", utc(11, 1, 7, 0)))
        # Spring gap skips 02:00 local; 01:30 quiet, 03:00 no longer quiet.
        self.assertFalse(policy.permits("rgb", utc(3, 8, 6, 30)))
        self.assertTrue(policy.permits("rgb", utc(3, 8, 7, 0)))
        window = QuietPolicy(windows=(QuietWindow(23 * 60, 7 * 60),), local_timezone=timezone.utc)
        clock = [utc(10, 3, 22, 59)]
        monotonic = [0.0]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0],
                           monotonic_clock=lambda: monotonic[0], policy=window)
        core.handle(event(clock[0]))
        clock[0] += 60
        monotonic[0] += 60
        core.tick()
        self.assertIsNone(rgb.state["rgb"])
        clock[0] -= 61
        monotonic[0] += 1
        core.tick()
        self.assertEqual(rgb.state["rgb"]["cue_id"], "job.completed")
        monotonic[0] += 240  # wall clock remains behind; lease still expires on elapsed time
        core.tick()
        self.assertIsNone(rgb.state["rgb"])


if __name__ == "__main__":
    unittest.main()
