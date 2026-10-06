"""Printer completion contract tests: fake normalized reports only."""

from datetime import datetime, timezone
import unittest

from starforge_cues import Coordinator, FakeSink
from starforge_cues.printer_status import BambuCompletionAdapter, PrinterObservation

NOW = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)


def report(sequence, state, job_id=None, *, epoch=1, at=NOW):
    return PrinterObservation(epoch, sequence, at, state, job_id)


class PrinterCompletionTests(unittest.TestCase):
    def make_adapter(self):
        return BambuCompletionAdapter("printer.bambu-p1s", clock=lambda: NOW.timestamp())

    def test_confirmed_finish_emits_one_semantic_event_without_job_name(self):
        adapter = self.make_adapter()
        self.assertEqual(adapter.observe(report(1, "printing", "opaque-run-1")).reason, "active")
        self.assertEqual(adapter.observe(report(2, "paused", "opaque-run-1")).reason, "paused")
        decision = adapter.observe(report(3, "finished", "opaque-run-1"))
        self.assertEqual(decision.outcome, "emitted")
        self.assertEqual((decision.event.cue_id, decision.event.status,
                          decision.event.severity, decision.event.text),
                         ("printer.completed", "succeeded", "info", "Print finished."))
        self.assertNotIn("opaque-run-1", repr(decision.event))
        sink = FakeSink()
        core = Coordinator({"text": sink}, clock=lambda: NOW.timestamp())
        self.assertEqual(core.handle(decision.event)["channels"]["text"], "accepted")
        self.assertEqual(adapter.observe(report(4, "finished", "opaque-run-1")).reason,
                         "duplicate_terminal")

    def test_explicit_failure_is_distinct_from_unknown_or_disconnect(self):
        adapter = self.make_adapter()
        self.assertEqual(adapter.observe(report(1, "printing", "job-fail")).outcome,
                         "suppressed")
        failed = adapter.observe(report(2, "failed", "job-fail"))
        self.assertEqual((failed.event.cue_id, failed.event.status, failed.event.severity),
                         ("printer.failed", "failed", "warning"))
        self.assertEqual(adapter.observe(report(3, "unknown")).reason, "unknown")
        self.assertEqual(adapter.observe(report(4, "failed", "other-run")).reason,
                         "unproven_terminal")
        self.assertEqual(adapter.observe(report(5, "idle")).outcome, "suppressed")

    def test_terminal_snapshot_and_reconnect_do_not_invent_completion(self):
        adapter = self.make_adapter()
        self.assertEqual(adapter.observe(report(1, "finished", "job-before-start")).reason,
                         "unproven_terminal")
        adapter.observe(report(2, "printing", "job-on-old-link"))
        self.assertEqual(adapter.observe(report(1, "finished", "job-on-old-link", epoch=2)).reason,
                         "unproven_terminal")
        self.assertEqual(adapter.observe(report(3, "printing", "job-on-old-link", epoch=1)).reason,
                         "old_connection")
        adapter.observe(report(2, "printing", "job-on-new-link", epoch=2))
        self.assertEqual(adapter.observe(report(3, "finished", "job-on-new-link", epoch=2)).outcome,
                         "emitted")

    def test_stale_future_out_of_order_and_unknown_break_transition(self):
        adapter = self.make_adapter()
        adapter.observe(report(1, "printing", "job-1"))
        self.assertEqual(adapter.observe(report(1, "finished", "job-1")).reason,
                         "out_of_order")
        old = datetime.fromtimestamp(NOW.timestamp() - 30, timezone.utc)
        self.assertEqual(adapter.observe(report(2, "finished", "job-1", at=old)).reason,
                         "out_of_order")
        self.assertEqual(adapter.observe(report(3, "finished", "job-1")).outcome,
                         "emitted")
        future = datetime.fromtimestamp(NOW.timestamp() + 6, timezone.utc)
        self.assertEqual(adapter.observe(report(4, "printing", "job-2", at=future)).reason,
                         "stale_or_future")
        adapter.observe(report(5, "printing", "job-2"))
        adapter.observe(report(6, "unknown"))
        self.assertEqual(adapter.observe(report(7, "failed", "job-2")).reason,
                         "unproven_terminal")

    def test_old_stale_packet_does_not_erase_newer_printing_proof(self):
        adapter = self.make_adapter()
        adapter.observe(report(5, "printing", "job-1"))
        old = datetime.fromtimestamp(NOW.timestamp() - 30, timezone.utc)
        self.assertEqual(adapter.observe(report(2, "printing", "job-1", at=old)).reason,
                         "out_of_order")
        self.assertEqual(adapter.observe(report(6, "finished", "job-1")).outcome,
                         "emitted")

    def test_in_order_stale_packet_breaks_printing_proof(self):
        clock = [NOW.timestamp()]
        adapter = BambuCompletionAdapter("printer.bambu-p1s", clock=lambda: clock[0])
        adapter.observe(report(1, "printing", "job-1"))
        clock[0] += 30
        delayed = datetime.fromtimestamp(NOW.timestamp() + 5, timezone.utc)
        self.assertEqual(adapter.observe(report(2, "printing", "job-1", at=delayed)).reason,
                         "stale_or_future")
        current = datetime.fromtimestamp(clock[0], timezone.utc)
        self.assertEqual(adapter.observe(report(3, "finished", "job-1", at=current)).reason,
                         "unproven_terminal")

    def test_job_switch_and_replay_ids_are_bounded(self):
        adapter = self.make_adapter()
        adapter.observe(report(1, "printing", "abandoned"))
        adapter.observe(report(2, "printing", "new-run"))
        self.assertEqual(adapter.observe(report(3, "finished", "abandoned")).reason,
                         "unproven_terminal")
        self.assertEqual(adapter.observe(report(4, "finished", "new-run")).reason,
                         "unproven_terminal")
        adapter.observe(report(5, "printing", "new-run"))
        completed = adapter.observe(report(6, "finished", "new-run")).event
        self.assertLessEqual(len(completed.event_id), 80)
        self.assertEqual(completed.event_id, completed.idempotency_key)
        self.assertEqual(adapter.observe(report(7, "failed", "new-run")).reason,
                         "duplicate_terminal")
        self.assertLessEqual(len(adapter._terminal), 256)

    def test_conflicting_terminal_invalidates_old_active_job(self):
        for conflict in ("finished", "failed"):
            with self.subTest(conflict=conflict):
                adapter = self.make_adapter()
                adapter.observe(report(1, "printing", "job-A"))
                self.assertEqual(adapter.observe(report(2, conflict, "job-B")).reason,
                                 "unproven_terminal")
                self.assertEqual(adapter.observe(report(3, "finished", "job-A")).reason,
                                 "unproven_terminal")
                adapter.observe(report(4, "printing", "job-A"))
                self.assertEqual(adapter.observe(report(5, "finished", "job-A")).outcome,
                                 "emitted")

    def test_old_or_reordered_conflict_cannot_override_new_evidence(self):
        adapter = self.make_adapter()
        adapter.observe(report(1, "printing", "old", epoch=1))
        adapter.observe(report(1, "printing", "current", epoch=2))
        self.assertEqual(adapter.observe(report(2, "finished", "old", epoch=1)).reason,
                         "old_connection")
        self.assertEqual(adapter.observe(report(1, "failed", "other", epoch=2)).reason,
                         "out_of_order")
        self.assertEqual(adapter.observe(report(2, "finished", "current", epoch=2)).outcome,
                         "emitted")

    def test_strict_normalized_contract(self):
        for value in (True, 0, 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PrinterObservation(value, 1, NOW, "printing", "job")
        for state, job in (("finished", None), ("unknown", "job"), ([], None)):
            with self.subTest(state=state), self.assertRaises(ValueError):
                PrinterObservation(1, 1, NOW, state, job)
        with self.assertRaises(ValueError):
            PrinterObservation(1, 1, datetime(2026, 10, 5, 20), "printing", "job")
        with self.assertRaises(TypeError):
            self.make_adapter().observe({"state": "finished"})
