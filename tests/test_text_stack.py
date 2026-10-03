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
        self.assertTrue(model.refresh(core.text_snapshot(), unlocked=False, now=NOW)["hidden"])
        self.assertEqual(len(model.refresh(core.text_snapshot(), unlocked=True, now=NOW)["rows"]), 1)

    def test_preempted_cue_remains_in_single_coordinator_stack(self):
        core = Coordinator(clock=lambda: NOW)
        core.handle(event(NOW, event_id="urgent", idempotency_key="urgent",
                          subject_id="urgent", severity="critical"))
        low = core.handle(event(NOW, event_id="low", idempotency_key="low",
                                subject_id="low", severity="info"))
        self.assertEqual(low["channels"]["text"], "preempted")
        self.assertEqual(len(TextStackModel().refresh(core.text_snapshot(), unlocked=True,
                                                      now=NOW)["rows"]), 2)

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
        view = model.refresh(snapshot, unlocked=True, now=clock[0])
        self.assertEqual(len(view["rows"]), 4)
        self.assertGreater(view["overflow"], 0)
        self.assertEqual(len(view["history"]), 10)
        self.assertIsNone(view["rows"][0]["text"])
        self.assertIn("source-1", view["rows"][0]["label"])
        self.assertNotIn("None", view["rows"][0]["label"])
        self.assertTrue(any(row["count"] > 1 for row in view["rows"] +
                            TextStackModel(max_visible=16).refresh(snapshot, unlocked=True,
                                                                     now=clock[0])["rows"]))
        self.assertTrue(all("text" not in record for record in view["history"]))

    def test_dismissal_expiry_and_duplicate_do_not_create_false_history(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        first = event(clock[0], event_id="first", idempotency_key="first",
                      subject_id="first", ttl_ms=1000)
        core.handle(first)
        self.assertEqual(core.handle(first)["reason"], "duplicate")
        model = TextStackModel()
        view = model.refresh(core.text_snapshot(), unlocked=True, now=clock[0])
        self.assertEqual(len(view["history"]), 1)
        model.dismiss(view["rows"][0]["row_id"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["rows"], [])
        clock[0] += 2
        core.tick()
        self.assertEqual(core.text_snapshot()["total"], 0)
        model.refresh(core.text_snapshot(), unlocked=True, now=clock[0])
        self.assertEqual(len(model._dismissed), 0)
        model.clear_history()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["history"], [])
        clock[0] += 61
        core.tick()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["history"], [])

    def test_quiet_lock_exclusion_and_unknown_remain_private(self):
        clock = [NOW]
        core = Coordinator(clock=lambda: clock[0])
        core.handle(event(clock[0], event_id="unknown", idempotency_key="unknown",
                          subject_id="unknown", source_id="private.source",
                          confidence="unknown", status="unknown", text=None))
        model = TextStackModel(excluded_sources=frozenset({"private.source"}))
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["rows"], [])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["history"], [])
        model = TextStackModel()
        self.assertTrue(model.refresh(core.text_snapshot(), unlocked=False,
                                      now=clock[0])["hidden"])
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["rows"][0]["status"], "unknown")
        self.assertIn("unknown provenance", model.refresh(core.text_snapshot(), unlocked=True,
                                                           now=clock[0])["rows"][0]["label"])
        core.set_policy(QuietPolicy(dnd=True))
        self.assertEqual(core.text_snapshot(), {"quiet": True, "total": 0, "entries": []})
        hidden = model.refresh(core.text_snapshot(), unlocked=True, now=clock[0])
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
        view = model.refresh(core.text_snapshot(), unlocked=True, now=clock[0])
        self.assertEqual(len(view["history"]), 2)
        self.assertEqual(len(core.text_snapshot(limit=1)["entries"]), 1)
        with self.assertRaises(ValueError):
            core.text_snapshot(limit=33)
        clock[0] += 61
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["history"], [])
        model.clear_history()
        self.assertEqual(model.refresh(core.text_snapshot(), unlocked=True,
                                       now=clock[0])["history"], [])

    def test_view_replaces_direction_controls_without_changing_event_contract(self):
        core = Coordinator(clock=lambda: NOW)
        core.handle(event(NOW, event_id="direction", idempotency_key="direction",
                          subject_id="direction", text="Synthetic\u202e body"))
        row = TextStackModel().refresh(core.text_snapshot(), unlocked=True, now=NOW)["rows"][0]
        self.assertNotIn("\u202e", row["label"])
        self.assertEqual(row["text"], "Synthetic\ufffd body")


if __name__ == "__main__":
    unittest.main()
