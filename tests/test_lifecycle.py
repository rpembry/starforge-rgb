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
    def test_decision_refresh_updates_text_without_replaying_sound(self):
        clock = [NOW]
        text, audio = FakeSink(), FakeSink()
        core = Coordinator({"text": text, "audio": audio}, clock=lambda: clock[0])
        first = core.handle(event(clock[0], event_id="decision-1", idempotency_key="decision-1",
                                  cue_id="agent.decision", subject_id="agent-run-1",
                                  status="needs_attention", text="Choose an option"))
        self.assertEqual(first["channels"]["audio"], "accepted")
        audio_count = len(audio.calls)
        clock[0] += 20
        refreshed = core.handle(event(clock[0], event_id="decision-2", idempotency_key="decision-2",
                                      cue_id="agent.decision.updated", subject_id="agent-run-1",
                                      status="needs_attention", text="Choose an updated option"))
        self.assertEqual(refreshed["channels"]["audio"], "suppressed")
        self.assertEqual(len(audio.calls), audio_count)
        self.assertEqual(text.state["text"]["text"], "Choose an updated option")
        self.assertEqual(refreshed["plan"]["status"], "needs_attention")
        clock[0] += 20
        resolved = core.handle(event(clock[0], event_id="decision-done", idempotency_key="decision-done",
                                     cue_id="agent.completed", subject_id="agent-run-1",
                                     status="succeeded", text="Decision resolved"))
        self.assertEqual(resolved["channels"]["audio"], "accepted")
        self.assertEqual(len(audio.calls), audio_count + 1)

    def test_ambiguous_first_decision_sound_is_not_retried_on_refresh(self):
        class AmbiguousAudio(FakeSink):
            def dispatch(self, channel, plan):
                self.calls.append((channel, dict(plan)))
                return "unknown"

        clock = [NOW]
        audio = AmbiguousAudio()
        core = Coordinator({"audio": audio}, clock=lambda: clock[0])
        first = core.handle(event(clock[0], event_id="first", idempotency_key="first",
                                  cue_id="agent.decision", subject_id="agent-run-1",
                                  status="needs_attention"))
        self.assertEqual(first["channels"]["audio"], "unknown")
        first_count = len(audio.calls)
        clock[0] += 10
        refreshed = core.handle(event(clock[0], event_id="renew", idempotency_key="renew",
                                      cue_id="agent.decision", subject_id="agent-run-1",
                                      status="needs_attention"))
        self.assertEqual(refreshed["channels"]["audio"], "suppressed")
        self.assertEqual(len(audio.calls), first_count)

    def test_renewal_horizon_requires_new_subject_after_cap(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0], max_lease_age_s=300)
        core.set_baseline(1, "ambient")
        core.handle(event(clock[0], event_id="first", idempotency_key="first",
                          subject_id="run-one", status="started"))
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

    def test_tiny_ttl_cannot_resurrect_same_subject(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="brief", idempotency_key="brief",
                          subject_id="run-one", ttl_ms=1))
        clock[0] += 0.002
        core.tick()
        resume = core.handle(event(clock[0], event_id="resume", idempotency_key="resume",
                                   subject_id="run-one", status="progress"))
        self.assertEqual(resume["reason"], "lease_limit")
        new_run = core.handle(event(clock[0], event_id="next", idempotency_key="next",
                                    subject_id="run-two", status="started"))
        self.assertEqual(new_run["result"], "accepted")

    def test_early_cancel_retires_subject(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="running", idempotency_key="running",
                          subject_id="run-one"))
        clock[0] += 100
        cancel = core.handle(event(clock[0], event_id="stop", idempotency_key="stop",
                                   subject_id="run-one", status="cancelled"))
        self.assertEqual(cancel["result"], "accepted")
        resume = core.handle(event(clock[0], event_id="resume", idempotency_key="resume",
                                   subject_id="run-one", status="progress"))
        self.assertEqual(resume["reason"], "lease_limit")
        self.assertEqual(core.handle(event(clock[0], event_id="new", idempotency_key="new",
                                           subject_id="run-two", status="started"))["result"], "accepted")

    def test_active_terminal_uses_separate_bounded_admission(self):
        clock = [NOW]
        audio = FakeSink()
        core = Coordinator({"audio": audio}, clock=lambda: clock[0], cooldown_seconds=60)
        for index in range(10):
            identifier = f"progress-{index}"
            result = core.handle(event(clock[0], event_id=identifier, idempotency_key=identifier,
                                       subject_id="run-one", status="started" if index == 0 else "progress"))
            # The cooldown blocks repeated progress, so use a second instance
            # below to exercise a full ten-event rate window.
            if index:
                self.assertEqual(result["reason"], "cooldown")
            else:
                self.assertEqual(result["result"], "accepted")
        terminal = event(clock[0], event_id="failed", idempotency_key="failed",
                         subject_id="run-one", status="failed", severity="critical")
        self.assertEqual(core.handle(terminal)["result"], "accepted")
        self.assertEqual(core.handle(terminal)["reason"], "duplicate")
        self.assertEqual(core.handle(event(clock[0], event_id="second-terminal",
                                           idempotency_key="second-terminal", subject_id="run-one",
                                           status="succeeded"))["reason"], "duplicate")
        self.assertEqual(core.handle(event(clock[0], event_id="resume",
                                           idempotency_key="resume", subject_id="run-one",
                                           status="progress"))["reason"], "lease_limit")
        self.assertEqual(len(core._terminal_rate["synthetic.build"]), 1)

        full = Coordinator(clock=lambda: clock[0])
        for index in range(10):
            identifier = f"update-{index}"
            self.assertEqual(full.handle(event(clock[0], event_id=identifier,
                                               idempotency_key=identifier, subject_id="other-run",
                                               status="progress"))["result"], "accepted")
        self.assertEqual(full.handle(event(clock[0], event_id="normal",
                                           idempotency_key="normal", subject_id="other-run",
                                           status="progress"))["reason"], "rate_limit")
        self.assertEqual(full.handle(event(clock[0], event_id="terminal",
                                           idempotency_key="terminal", subject_id="other-run",
                                           status="failed"))["result"], "accepted")
        self.assertEqual(len(full._rate["synthetic.build"]), 10)

    def test_terminal_does_not_revive_expired_or_horizon_limited_subject(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="short", idempotency_key="short",
                          subject_id="short", status="started", ttl_ms=1))
        clock[0] += 0.002
        expired = core.handle(event(clock[0], event_id="short-failed",
                                    idempotency_key="short-failed", subject_id="short",
                                    status="failed"))
        self.assertEqual(expired["reason"], "lease_limit")
        self.assertEqual(len(core._terminal_rate), 0)
        self.assertEqual(len(core._rate["synthetic.build"]), 1)
        core.handle(event(clock[0], event_id="long", idempotency_key="long",
                          subject_id="long", status="started"))
        clock[0] += 301
        limited = core.handle(event(clock[0], event_id="long-failed",
                                    idempotency_key="long-failed", subject_id="long",
                                    status="failed"))
        self.assertEqual(limited["reason"], "lease_limit")
        self.assertEqual(len(core._terminal_rate), 0)

    def test_terminal_reserve_has_per_source_rate_bound(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        for index in range(21):
            if index in (10, 20):
                clock[0] += 61
            identifier = f"run-{index}"
            self.assertEqual(core.handle(event(clock[0], event_id=identifier,
                                               idempotency_key=identifier,
                                               subject_id=identifier, status="started"))["result"],
                             "accepted")
        for index in range(21):
            identifier = f"finish-{index}"
            result = core.handle(event(clock[0], event_id=identifier,
                                       idempotency_key=identifier,
                                       subject_id=f"run-{index}", status="succeeded"))
            self.assertEqual(result["result"] if index < 20 else result["reason"],
                             "accepted" if index < 20 else "rate_limit")
        self.assertEqual(len(core._terminal_rate["synthetic.build"]), 20)

    def test_terminal_can_use_replay_capacity_reserved_from_progress(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        first = event(clock[0], event_id="started", idempotency_key="started",
                      subject_id="run-one", status="started")
        core.handle(first)
        for index in range(3583):
            core._seen[("other.source", f"reserved-{index}")] = (NOW + 240, first)
        progress = event(clock[0], event_id="progress", idempotency_key="progress",
                         subject_id="run-one", status="progress")
        self.assertEqual(core.handle(progress)["reason"], "capacity")
        terminal = event(clock[0], event_id="terminal", idempotency_key="terminal",
                         subject_id="run-one", status="failed")
        self.assertEqual(core.handle(terminal)["result"], "accepted")
        self.assertEqual(len(core._seen), 3585)

    def test_active_terminal_reserve_survives_full_generation_budget(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="started", idempotency_key="started",
                          subject_id="active-run", status="started"))
        for index in range(2047):
            core._retired_subjects[("other.source", f"subject:retired-{index}")] = NOW + 600
        for index in range(1, 10):
            identifier = f"progress-{index}"
            self.assertEqual(core.handle(event(clock[0], event_id=identifier,
                                               idempotency_key=identifier,
                                               subject_id="active-run", status="progress"))["result"],
                             "accepted")
        self.assertEqual(core.handle(event(clock[0], event_id="new-run",
                                           idempotency_key="new-run", subject_id="new-run",
                                           status="started"))["reason"], "capacity")
        self.assertEqual(core.handle(event(clock[0], event_id="failed",
                                           idempotency_key="failed", subject_id="active-run",
                                           status="failed"))["result"], "accepted")
        self.assertEqual(core.handle(event(clock[0], event_id="one-shot",
                                           idempotency_key="one-shot", source_id="other.one-shot",
                                           subject_id=None))["result"],
                         "accepted")

    def test_cancel_transfers_reserved_subject_slot_at_capacity(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="active", idempotency_key="active",
                          subject_id="run-one"))
        for index in range(2047):
            core._retired_subjects[("other.source", f"subject:retired-{index}")] = NOW + 600
        cancel = core.handle(event(clock[0], event_id="stop", idempotency_key="stop",
                                   subject_id="run-one", status="cancelled"))
        self.assertEqual(cancel["result"], "accepted")
        self.assertEqual(len(core._retired_subjects), 2048)
        self.assertEqual(core.handle(event(clock[0], event_id="next", idempotency_key="next",
                                           subject_id="run-two"))["reason"], "capacity")
        self.assertIn(("other.source", "subject:retired-0"), core._retired_subjects)
        self.assertEqual(core.handle(event(clock[0], event_id="one-shot",
                                           idempotency_key="one-shot", subject_id=None))["result"],
                         "accepted")

    def test_active_generation_keeps_original_age_at_subject_capacity(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        core.handle(event(clock[0], event_id="first", idempotency_key="first",
                          subject_id="protected", status="started"))
        for index in range(2047):
            core._retired_subjects[("other.source", f"subject:retired-{index}")] = NOW + 600
        clock[0] += 200
        renewed = core.handle(event(clock[0], event_id="renew", idempotency_key="renew",
                                    subject_id="protected", status="progress"))
        self.assertEqual(renewed["result"], "accepted")
        self.assertEqual(core._leases[("synthetic.build", "subject:protected")].first_seen, NOW)
        self.assertEqual(core._leases[("synthetic.build", "subject:protected")].expiry, NOW + 300)
        clock[0] += 101
        core.tick()
        self.assertEqual(core.handle(event(clock[0], event_id="resume", idempotency_key="resume",
                                           subject_id="protected", status="progress"))["reason"],
                         "lease_limit")

    def test_unknown_cancellation_flood_preserves_replay_record(self):
        clock = [NOW]
        audio = FakeSink()
        core = Coordinator({"audio": audio}, clock=lambda: clock[0])
        original = event(clock[0], event_id="original", idempotency_key="original",
                         subject_id="real", ttl_ms=300000)
        self.assertEqual(core.handle(original)["result"], "accepted")
        sound_count = len(audio.calls)
        for index in range(4096):
            missing = event(clock[0], event_id=f"cancel-{index}",
                            idempotency_key=f"cancel-{index}", source_id="other.source",
                            subject_id=f"missing-{index}", status="cancelled", ttl_ms=300000)
            self.assertEqual(core.handle(missing)["reason"], "unknown_subject")
        self.assertEqual(len(core._seen), 1)
        self.assertEqual(core.handle(original)["reason"], "duplicate")
        self.assertEqual(len(audio.calls), sound_count)

    def test_short_lived_subject_churn_cannot_reset_unexpired_generation(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        for index in range(2048):
            identifier = f"short-{index}"
            self.assertEqual(core.handle(event(clock[0], event_id=identifier,
                                               idempotency_key=identifier,
                                               source_id=f"source-{index % 256}",
                                               subject_id=identifier, ttl_ms=1))["result"], "accepted")
            clock[0] += 0.002
        core.tick()
        self.assertEqual(len(core._retired_subjects), 2048)
        self.assertEqual(core.handle(event(clock[0], event_id="over-cap",
                                           idempotency_key="over-cap", source_id="source-0",
                                           subject_id="new-run", ttl_ms=1))["reason"], "capacity")
        self.assertIn(("source-0", "subject:short-0"), core._retired_subjects)
        self.assertEqual(core.handle(event(clock[0], event_id="old-id",
                                           idempotency_key="old-id", source_id="source-0",
                                           subject_id="short-0"))["reason"], "lease_limit")
        self.assertEqual(core.handle(event(clock[0], event_id="fresh", idempotency_key="fresh",
                                           source_id="source-0", subject_id=None,
                                           ttl_ms=1))["result"], "accepted")
        clock[0] += 601
        core.tick()
        self.assertEqual(core.handle(event(clock[0], event_id="after-horizon",
                                           idempotency_key="after-horizon", source_id="source-0",
                                           subject_id="short-0"))["result"], "accepted")

    def test_expired_one_shots_do_not_starve_unrelated_traffic(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        for index in range(4096):
            # Stay below the ten-per-source/minute rate limit across 256 sources.
            if index == 2560:
                clock[0] += 61
            identifier = f"one-shot-{index}"
            self.assertEqual(core.handle(event(clock[0], event_id=identifier,
                                               idempotency_key=identifier,
                                               source_id=f"source-{index % 256}",
                                               subject_id=None, ttl_ms=1))["result"], "accepted")
            clock[0] += 0.002
        core.tick()
        self.assertEqual(len(core._leases), 0)
        self.assertEqual(len(core._retired_subjects), 0)
        self.assertEqual(core.handle(event(clock[0], event_id="fresh", idempotency_key="fresh",
                                           source_id="source-0", subject_id=None,
                                           ttl_ms=1))["result"], "accepted")

    def test_one_shot_and_subject_namespaces_are_independent(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0], max_lease_age_s=300)
        self.assertEqual(core.handle(event(clock[0], event_id="running", idempotency_key="running",
                                           subject_id="shared", ttl_ms=1))["result"], "accepted")
        clock[0] += 0.002
        core.tick()
        self.assertEqual(core.handle(event(clock[0], event_id="resume", idempotency_key="resume",
                                           subject_id="shared"))["reason"], "lease_limit")
        self.assertEqual(core.handle(event(clock[0], event_id="shared", idempotency_key="one-shot",
                                           subject_id=None, ttl_ms=1))["result"], "accepted")
        self.assertEqual(core.handle(event(clock[0], event_id="another", idempotency_key="another",
                                           subject_id="shared"))["reason"], "lease_limit")

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
