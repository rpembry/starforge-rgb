"""Synthetic stack, privacy, and lifecycle tests without a desktop session."""

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from starforge_cues import Coordinator, CueEvent, QuietPolicy
from starforge_cues.text_stack import TextProjectionSink, TextStackModel

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()


def event(at, **changes):
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return CueEvent.from_mapping({**FIXTURE, "occurred_at": stamp, "observed_at": stamp, **changes})


class TextStackTests(unittest.TestCase):
    def test_projection_sink_receipt_is_distinct_from_view_visibility(self):
        sink = TextProjectionSink()
        core = Coordinator({"text": sink}, clock=lambda: NOW)
        result = core.handle(event(NOW, event_id="visible", idempotency_key="visible",
                                   subject_id="visible"))
        self.assertEqual(result["channels"]["text"], "accepted")
        self.assertEqual(sink.last_plan["cue_id"], "job.completed")
        model = TextStackModel()
        self.assertTrue(model.refresh(core.text_snapshot(), unlocked=False, elapsed=NOW)["hidden"])
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True, elapsed=NOW)["rows"]), 1)

    def test_preempted_cue_remains_in_single_coordinator_stack(self):
        core = Coordinator(clock=lambda: NOW)
        core.handle(event(NOW, event_id="urgent", idempotency_key="urgent",
                          subject_id="urgent", severity="critical"))
        low = core.handle(event(NOW, event_id="low", idempotency_key="low",
                                subject_id="low", severity="info"))
        self.assertEqual(low["channels"]["text"], "preempted")
        self.assertEqual(len(TextStackModel().refresh(core.text_snapshot(), unlocked=True,
                                                      elapsed=NOW)["rows"]), 2)

    def test_stack_group_burst_bound_and_sound_only_confidence(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        for index in range(10):
            core.handle(event(clock[0], event_id=f"burst-{index}",
                              idempotency_key=f"burst-{index}", subject_id=f"run-{index}",
                              source_id=f"source-{index % 2}",
                              metadata={"group": "batch"} if index < 3 else {},
                              text=None if index == 9 else f"Synthetic update {index}"))
        snapshot = core.text_snapshot()
        self.assertEqual(snapshot["total"], 10)
        model = TextStackModel(max_visible=4)
        view = model.refresh(snapshot, unlocked=True, elapsed=clock[0])
        self.assertEqual(len(view["rows"]), 4)
        self.assertGreater(view["overflow"], 0)
        self.assertEqual(len(view["history"]), 10)
        self.assertIsNone(view["rows"][0]["text"])
        self.assertIn("source-1", view["rows"][0]["label"])
        self.assertNotIn("None", view["rows"][0]["label"])
        self.assertTrue(any(row["count"] > 1 for row in view["rows"] +
                            TextStackModel(max_visible=16).refresh(snapshot, unlocked=True,
                                                                     elapsed=clock[0])["rows"]))
        self.assertTrue(all("text" not in record for record in view["history"]))

    def test_dismissal_expiry_and_duplicate_do_not_create_false_history(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        first = event(clock[0], event_id="first", idempotency_key="first",
                      subject_id="first", ttl_ms=1000)
        core.handle(first)
        self.assertEqual(core.handle(first)["reason"], "duplicate")
        model = TextStackModel()
        view = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        self.assertEqual(len(view["history"]), 1)
        model.dismiss(view["rows"][0]["row_id"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["rows"], [])
        clock[0] += 2
        core.tick()
        self.assertEqual(core.text_snapshot()["total"], 0)
        model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        self.assertEqual(len(model._dismissed), 0)
        model.clear_history()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["history"], [])

    def test_new_failed_revision_surfaces_after_progress_dismissal(self):
        core = Coordinator(clock=lambda: NOW)
        model = TextStackModel()
        progress = event(NOW, event_id="progress", idempotency_key="progress",
                         subject_id="run-one", status="progress", severity="info")
        core.handle(progress)
        first = model.refresh(core.text_snapshot(), unlocked=True, elapsed=0)
        model.dismiss(first["rows"][0]["row_id"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True, elapsed=0)["rows"], [])
        failed = event(NOW, event_id="failed", idempotency_key="failed",
                       subject_id="run-one", status="failed", severity="critical")
        self.assertEqual(core.handle(failed)["result"], "accepted")
        updated = model.refresh(core.text_snapshot(), unlocked=True, elapsed=1)
        self.assertEqual(updated["rows"][0]["status"], "failed")
        self.assertEqual(updated["rows"][0]["severity"], "critical")
        self.assertEqual([item["status"] for item in updated["history"]],
                         ["failed", "progress"])
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True,
                                           elapsed=2)["history"]), 2)
        model.dismiss(updated["rows"][0]["row_id"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True, elapsed=2)["rows"], [])

    def test_quiet_lock_exclusion_and_unknown_remain_private(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        core.handle(event(clock[0], event_id="unknown", idempotency_key="unknown",
                          subject_id="unknown", source_id="private.source",
                          confidence="unknown", status="unknown", text=None))
        model = TextStackModel(excluded_sources=frozenset({"private.source"}))
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["rows"], [])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["history"], [])
        model = TextStackModel()
        self.assertTrue(model.refresh(core.text_snapshot(), unlocked=False,
                                      elapsed=clock[0])["hidden"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["rows"][0]["status"], "unknown")
        self.assertIn("unknown provenance", model.refresh(core.text_snapshot(), unlocked=True,
                                                           elapsed=clock[0])["rows"][0]["label"])
        core.set_policy(QuietPolicy(dnd=True))
        self.assertEqual(core.text_snapshot(), {"quiet": True, "total": 0,
                                                "source_totals": {},
                                                "active_revisions": [], "entries": []})
        hidden = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        self.assertTrue(hidden["hidden"])
        self.assertEqual(hidden["history"], [])

    def test_history_limits_clear_and_snapshot_bounds(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        model = TextStackModel(max_history=2, history_age_s=60)
        for index in range(3):
            core.handle(event(clock[0], event_id=f"history-{index}",
                              idempotency_key=f"history-{index}",
                              subject_id=f"history-{index}"))
        view = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        self.assertEqual(len(view["history"]), 2)
        self.assertEqual(len(core.text_snapshot(limit=1)["entries"]), 1)
        with self.assertRaises(ValueError):
            core.text_snapshot(limit=33)
        clock[0] += 61
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["history"], [])
        model.clear_history()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["history"], [])

    def test_history_uses_elapsed_time_across_backward_wall_jump(self):
        wall = [NOW]
        elapsed = [0.0]
        core = Coordinator(clock=lambda: wall[0], monotonic_clock=lambda: elapsed[0])
        core.handle(event(wall[0], event_id="timed", idempotency_key="timed",
                          subject_id="timed"))
        model = TextStackModel(history_age_s=60)
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True,
                                           elapsed=elapsed[0])["history"]), 1)
        wall[0] -= 7200
        elapsed[0] += 61
        self.assertEqual(core.text_snapshot()["total"], 1)
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=elapsed[0])["history"], [])

    def test_snapshot_truncation_and_source_exclusion_are_honest(self):
        core = Coordinator(clock=lambda: NOW)
        for index in range(40):
            self.assertEqual(core.handle(event(NOW, event_id=f"many-{index}",
                                               idempotency_key=f"many-{index}",
                                               subject_id=f"many-{index}",
                                               source_id=f"source-{index % 4}"))["result"], "accepted")
        snapshot = core.text_snapshot()
        self.assertEqual(snapshot["total"], 40)
        self.assertEqual(len(snapshot["entries"]), 32)
        self.assertEqual(len(snapshot["active_revisions"]), 40)
        view = TextStackModel().refresh(snapshot, unlocked=True, elapsed=0)
        self.assertEqual(len(view["rows"]), 8)
        self.assertEqual(view["overflow"], 24)
        self.assertEqual(view["truncated"], 8)
        excluded = TextStackModel(excluded_sources=frozenset({"source-0"}))
        filtered = excluded.refresh(snapshot, unlocked=True, elapsed=0)
        self.assertEqual(filtered["truncated"], 6)
        self.assertTrue(all(item["source_id"] != "source-0" for item in filtered["history"]))

    def test_truncated_projection_preserves_dismissal_and_history_until_real_expiry(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        model = TextStackModel()
        core.handle(event(clock[0], event_id="low", idempotency_key="low",
                          subject_id="low", source_id="low.source", severity="info"))
        first = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        model.dismiss(first["rows"][0]["row_id"])
        for index in range(32):
            core.handle(event(clock[0], event_id=f"critical-{index}",
                              idempotency_key=f"critical-{index}",
                              subject_id=f"critical-{index}",
                              source_id=f"critical.source-{index}",
                              severity="critical", ttl_ms=1))
        truncated = core.text_snapshot()
        self.assertEqual(truncated["total"], 33)
        self.assertEqual(len(truncated["entries"]), 32)
        self.assertEqual(len(truncated["active_revisions"]), 33)
        model.refresh(truncated, unlocked=True, elapsed=clock[0])
        self.assertEqual(len(model._dismissed), 1)
        clock[0] += 0.002
        core.tick()
        returned = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        self.assertEqual(returned["rows"], [])
        self.assertEqual(sum(item["entry_id"].endswith("subject:low")
                             for item in returned["history"]), 1)

    def test_identity_metadata_stays_bounded_under_truncated_churn(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        model = TextStackModel()
        core.handle(event(clock[0], event_id="low", idempotency_key="low",
                          subject_id="low", source_id="low.source"))
        first = model.refresh(core.text_snapshot(), unlocked=True, elapsed=clock[0])
        model.dismiss(first["rows"][0]["row_id"])
        for wave in range(20):
            for index in range(32):
                number = wave * 32 + index
                core.handle(event(clock[0], event_id=f"brief-{number}",
                                  idempotency_key=f"brief-{number}",
                                  subject_id=f"brief-{number}",
                                  source_id=f"source-{number % 255}",
                                  severity="critical", ttl_ms=1))
            snapshot = core.text_snapshot()
            self.assertEqual(snapshot["total"], 33)
            model.refresh(snapshot, unlocked=True, elapsed=clock[0])
            self.assertLessEqual(len(model._dismissed), 1)
            self.assertLessEqual(len(model._seen_history), 33)
            self.assertLessEqual(len(model._history), 50)
            clock[0] += 0.002
            core.tick()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=clock[0])["rows"], [])

    def test_changing_exclusions_purges_existing_history(self):
        core = Coordinator(clock=lambda: NOW)
        core.handle(event(NOW, event_id="private", idempotency_key="private",
                          subject_id="private", source_id="private.source"))
        model = TextStackModel()
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True,
                                           elapsed=0)["history"]), 1)
        model.set_excluded_sources(frozenset({"private.source"}))
        hidden = model.refresh(core.text_snapshot(), unlocked=True, elapsed=1)
        self.assertEqual(hidden["rows"], [])
        self.assertEqual(hidden["history"], [])
        self.assertEqual(model._history, [])
        with self.assertRaises(AttributeError):
            model.excluded_sources = frozenset()
        model.set_excluded_sources(frozenset())
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True,
                                           elapsed=2)["history"]), 1)
        model.clear_history()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       elapsed=2)["history"], [])

    def test_view_replaces_direction_controls_without_changing_event_contract(self):
        core = Coordinator(clock=lambda: NOW)
        core.handle(event(NOW, event_id="direction", idempotency_key="direction",
                          subject_id="direction", text="Synthetic\u202e body"))
        row = TextStackModel().refresh(core.text_snapshot(), unlocked=True, elapsed=NOW)["rows"][0]
        self.assertNotIn("\u202e", row["label"])
        self.assertEqual(row["text"], "Synthetic\ufffd body")


if __name__ == "__main__":
    unittest.main()
