import json
from pathlib import Path
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


class PolicyTests(unittest.TestCase):
    def make_core(self, **kw):
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        return Coordinator(sinks, clock=lambda: NOW, **kw), sinks

    def test_accept_duplicate_and_stale(self):
        core, _ = self.make_core()
        self.assertEqual(core.handle(event())["result"], "accepted")
        self.assertEqual(core.handle(event())["reason"], "duplicate")
        self.assertEqual(core.handle(event(idempotency_key="other"))["reason"], "duplicate")
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


if __name__ == "__main__":
    unittest.main()
