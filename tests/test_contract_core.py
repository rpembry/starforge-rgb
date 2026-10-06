import json
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from starforge_cues import ContractError, Coordinator, CueEvent, FakeSink, QuietPolicy, SourceCapabilities

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = 1790985660.0  # 2026-10-03 00:01 UTC


def event(**changes):
    return CueEvent.from_mapping({**FIXTURE, **changes})


class ContractTests(unittest.TestCase):
    def test_valid_and_missing_text(self):
        self.assertEqual(event().confidence, "known")
        self.assertIsNone(event(text=None).text)

    def test_invalid_fields_and_types(self):
        for change in ({"version": 2}, {"ttl_ms": True}, {"ttl_ms": 300001},
                       {"severity": "fatal"}, {"severity": []}, {"confidence": {}},
                       {"device": "keyboard"}, {"status": "done"}, {"status": []},
                       {"text": "x" * 281}, {"metadata": {"x": []}},
                       {"occurred_at": "yesterday"}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                event(**change)

    def test_oversized_and_duplicate_json_keys(self):
        with self.assertRaises(ContractError):
            CueEvent.from_json(b" " * 8193)
        with self.assertRaises(ContractError):
            CueEvent.from_json(b'{"version":1,"version":1}')

    def test_cli_diagnostic_redacts_untrusted_key(self):
        from starforge_cues.cli import main
        attacker_key = "private-message-" + "x" * 2000
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invalid.json"
            path.write_text(json.dumps({**FIXTURE, attacker_key: "private value"}))
            diagnostic = io.StringIO()
            with redirect_stderr(diagnostic):
                self.assertEqual(main(["dry-run", "--file", str(path)]), 2)
        self.assertNotIn(attacker_key, diagnostic.getvalue())
        self.assertNotIn("private value", diagnostic.getvalue())
        self.assertLess(len(diagnostic.getvalue()), 100)


class PolicyTests(unittest.TestCase):
    def make_core(self, **kw):
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        return Coordinator(sinks, clock=lambda: NOW, **kw), sinks

    def test_accept_duplicate_and_stale(self):
        core, _ = self.make_core()
        self.assertEqual(core.handle(event())["result"], "accepted")
        self.assertEqual(core.handle(event())["reason"], "duplicate")
        self.assertEqual(core.handle(event(idempotency_key="other"))["reason"], "replay_conflict")
        self.assertEqual(core.handle(event(occurred_at="2026-10-02T23:50:00Z"))["reason"], "stale")

    def test_quiet_independent_failures_and_absent_text(self):
        core, sinks = self.make_core(policy=QuietPolicy(quiet=True))
        self.assertEqual(set(core.handle(event())["channels"].values()), {"suppressed"})
        core, sinks = self.make_core()
        sinks["audio"].fail = frozenset({"audio"})
        result = core.handle(event(text=None, confidence="unknown", status="unknown"))
        self.assertEqual(result["channels"], {"text": "absent", "rgb": "accepted", "audio": "failed"})
        self.assertEqual(result["plan"]["status"], "unknown")
        self.assertEqual(result["plan"]["confidence"], "unknown")

    def test_preemption_cancellation_and_expiry(self):
        clock = [NOW]
        core = Coordinator({name: FakeSink() for name in ("text", "rgb", "audio")}, clock=lambda: clock[0])
        critical = event(event_id="urgent", idempotency_key="urgent", subject_id="urgent", severity="critical")
        self.assertEqual(core.handle(critical)["plan"]["baseline"], "job.completed")
        low = event(event_id="low", idempotency_key="low", subject_id="low", cue_id="job.progress")
        self.assertEqual(set(core.handle(low)["channels"].values()), {"preempted"})
        cancelled = event(event_id="cancel", idempotency_key="cancel", subject_id="urgent", status="cancelled")
        self.assertEqual(core.handle(cancelled)["plan"]["baseline"], "job.progress")
        clock[0] = NOW + 250
        self.assertEqual(core.handle(event(event_id="next", idempotency_key="next"))["reason"], "stale")

    def test_rate_limit_and_unsupported(self):
        core = Coordinator({"rgb": FakeSink()}, clock=lambda: NOW)
        for i in range(10):
            result = core.handle(event(event_id=f"e{i}", idempotency_key=f"k{i}"))
            self.assertEqual(result["channels"]["text"], "unsupported")
        self.assertEqual(core.handle(event(event_id="eleven", idempotency_key="eleven"))["reason"], "rate_limit")

    def test_source_capability_is_host_registered(self):
        caps = SourceCapabilities(frozenset({"unknown"}), offers_text=False, offers_subject=False)
        core = Coordinator(clock=lambda: NOW, sources={"synthetic.build": caps})
        self.assertEqual(core.handle(event())["reason"], "source_capability")
        self.assertEqual(core.handle(event(status="unknown", subject_id=None, text=None))["result"], "accepted")

    def test_replay_body_conflict_and_freshness(self):
        core, _ = self.make_core()
        self.assertEqual(core.handle(event())["result"], "accepted")
        self.assertEqual(core.handle(event(event_id="changed"))["reason"], "replay_conflict")
        self.assertEqual(core.handle(event(text="Different synthetic text"))["reason"], "replay_conflict")
        with self.assertRaises(ContractError):
            event(observed_at="2020-01-01T00:00:00Z")

    def test_restore_and_clear_are_dispatched(self):
        clock = [NOW]
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: clock[0])
        low = event(event_id="low", idempotency_key="low", subject_id="low", cue_id="job.progress")
        urgent = event(event_id="urgent", idempotency_key="urgent", subject_id="urgent", severity="critical")
        core.handle(low)
        core.handle(urgent)
        result = core.handle(event(event_id="cancel", idempotency_key="cancel", subject_id="urgent", status="cancelled"))
        self.assertEqual(result["plan"]["operation"], "restore")
        self.assertEqual(sinks["rgb"].state["rgb"]["cue_id"], "job.progress")
        clock[0] += 241
        tick = core.tick()
        self.assertEqual(tick["plan"]["operation"], "clear")
        self.assertIsNone(sinks["rgb"].state["rgb"])

    def test_expiry_clear_runs_during_quiet_policy(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0])
        core.handle(event())
        core.policy = QuietPolicy(quiet=True)
        clock[0] += 241
        self.assertEqual(core.tick()["channels"]["rgb"], "accepted")
        self.assertIsNone(rgb.state["rgb"])

    def test_restore_without_text_clears_preempting_text(self):
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: NOW)
        low = event(event_id="low", idempotency_key="low", subject_id="low",
                    cue_id="job.progress", text=None)
        high = event(event_id="high", idempotency_key="high", subject_id="high",
                     cue_id="urgent", severity="critical", text="High text")
        core.handle(low)
        core.handle(high)
        self.assertEqual(sinks["text"].state["text"]["text"], "High text")
        result = core.handle(event(event_id="cancel", idempotency_key="cancel",
                                   subject_id="high", status="cancelled"))
        self.assertEqual(result["channels"]["text"], "accepted")
        self.assertIsNone(sinks["text"].state["text"])
        self.assertEqual(sinks["rgb"].state["rgb"]["cue_id"], "job.progress")

    def test_failed_clear_retries_only_failed_channel(self):
        clock = [NOW]
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: clock[0])
        core.handle(event())
        sinks["rgb"].fail = frozenset({"rgb"})
        clock[0] += 241
        first = core.tick()
        self.assertEqual(first["channels"]["rgb"], "failed")
        self.assertIsNotNone(sinks["rgb"].state["rgb"])
        audio_calls = len(sinks["audio"].calls)
        text_calls = len(sinks["text"].calls)
        sinks["rgb"].fail = frozenset()
        retry = core.tick()
        self.assertEqual(retry["channels"], {"rgb": "accepted"})
        self.assertIsNone(sinks["rgb"].state["rgb"])
        self.assertEqual(len(sinks["audio"].calls), audio_calls)
        self.assertEqual(len(sinks["text"].calls), text_calls)
        self.assertEqual(core.tick()["result"], "unchanged")

    def test_reconciliation_retry_is_bounded(self):
        clock = [NOW]
        rgb = FakeSink()
        core = Coordinator({"rgb": rgb}, clock=lambda: clock[0])
        core.handle(event())
        rgb.fail = frozenset({"rgb"})
        clock[0] += 241
        self.assertEqual(core.tick()["channels"]["rgb"], "failed")
        self.assertEqual(core.tick()["channels"]["rgb"], "failed")
        self.assertEqual(core.tick()["channels"]["rgb"], "failed")
        count = len(rgb.calls)
        self.assertEqual(core.tick()["result"], "unchanged")
        self.assertEqual(len(rgb.calls), count)

    def test_failed_audio_restore_is_not_replayed(self):
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: NOW)
        core.handle(event(event_id="low", idempotency_key="low", subject_id="low",
                          cue_id="job.progress"))
        core.handle(event(event_id="high", idempotency_key="high", subject_id="high",
                          cue_id="urgent", severity="critical"))
        sinks["audio"].fail = frozenset({"audio"})
        result = core.handle(event(event_id="cancel", idempotency_key="cancel",
                                   subject_id="high", status="cancelled"))
        self.assertEqual(result["channels"]["audio"], "failed")
        self.assertEqual(sinks["audio"].calls[-1][1]["operation"], "restore")
        audio_calls = len(sinks["audio"].calls)
        sinks["audio"].fail = frozenset()
        self.assertEqual(core.tick()["result"], "unchanged")
        self.assertEqual(len(sinks["audio"].calls), audio_calls)

    def test_cancellation_admitted_at_capacity(self):
        core, sinks = self.make_core()
        core.handle(event())
        original = event()
        for i in range(4095):
            key = ("synthetic.build", f"filled{i}")
            core._seen[key] = (NOW + 240, original)
        core._rate = {f"source{i}": [NOW] for i in range(256)}
        result = core.handle(event(event_id="cancel", idempotency_key="cancel",
                                   subject_id="example-job", status="cancelled"))
        self.assertEqual(result["result"], "accepted")
        self.assertEqual(result["plan"]["operation"], "clear")
        self.assertIsNone(sinks["rgb"].state["rgb"])
        self.assertLessEqual(len(core._seen), 4096)
        self.assertIn((original.source_id, original.idempotency_key), core._seen)

    def test_redacted_unknown_fields_and_metadata(self):
        attacker_key = "x" * 2000
        with self.assertRaises(ContractError) as captured:
            event(**{attacker_key: "secret"})
        self.assertNotIn(attacker_key, str(captured.exception))
        self.assertLess(len(str(captured.exception)), 100)
        for key in ("device", "command", "rgb", "path"):
            with self.assertRaises(ContractError):
                event(metadata={key: "arbitrary"})


if __name__ == "__main__":
    unittest.main()
