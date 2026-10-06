"""Proposed binding and frame types use synthetic data only; no listener."""

import json
from pathlib import Path
import unittest

from starforge_cues.contract import ContractError
from starforge_cues.ingress_contract import (
    AdmittedTransition, GenerationIds, GenerationRef, HelloFrame,
    IngressBinding, PublishFrame, RetractFrame, authorize_frame, parse_frame,
)


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
EPOCH = "0123456789abcdef0123456789abcdef"


def binding(**changes):
    return IngressBinding(**{"binding_id": "synthetic.binding",
                             "source_ids": frozenset({"synthetic.build"}),
                             "statuses": frozenset({"started", "succeeded", "cancelled"}),
                             "max_severity": "warning", "max_confidence": "known",
                             "offers_text": True, "offers_subject": True,
                             "can_retract": True, **changes})


class IngressContractTests(unittest.TestCase):
    def test_generation_is_host_epoch_and_ordinal_not_reusable_subject_name(self):
        allocator = GenerationIds(EPOCH)
        first, second = allocator.allocate(), allocator.allocate()
        self.assertEqual((first.ordinal, second.ordinal), (1, 2))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, GenerationRef("f" * 32, 1))
        transition = AdmittedTransition(first, 7, "event-one", "succeeded", "started")
        self.assertTrue(transition.matches(first, 7, "event-one", "succeeded"))
        self.assertFalse(transition.matches(second, 7, "event-one", "succeeded"))
        self.assertFalse(transition.matches(first, 8, "event-one", "succeeded"))
        self.assertFalse(transition.matches(first, 7, "other-event", "succeeded"))
        self.assertFalse(transition.matches(first, 7, "event-one", "failed"))
        for epoch, ordinal in (("bad", 1), (EPOCH, 0), (EPOCH, True), (EPOCH, 2**63)):
            with self.subTest(epoch=epoch, ordinal=ordinal), self.assertRaises(ValueError):
                GenerationRef(epoch, ordinal)

    def test_strict_publish_retract_hello_frames_are_data_only(self):
        publish = parse_frame({"proto": 1, "op": "publish", "event": FIXTURE})
        retract = parse_frame({"proto": 1, "op": "retract", "source_id": "synthetic.build",
                               "subject_id": "example-job", "request_id": "retract-one"})
        hello = parse_frame({"proto": 1, "op": "hello"})
        self.assertIsInstance(publish, PublishFrame)
        self.assertIsInstance(retract, RetractFrame)
        self.assertIsInstance(hello, HelloFrame)
        self.assertEqual(authorize_frame(binding(), publish).binding_id, "synthetic.binding")
        self.assertEqual(authorize_frame(binding(), retract).frame, retract)
        self.assertEqual(authorize_frame(binding(), hello).frame, hello)
        for frame in ({"proto": True, "op": "hello"}, {"proto": 2, "op": "hello"},
                      {"proto": 1, "op": "hello", "token": "no"},
                      {"proto": 1, "op": "retract", "source_id": "synthetic.build"},
                      {"proto": 1, "op": "unknown"}):
            with self.subTest(frame=frame), self.assertRaises(ContractError):
                parse_frame(frame)

    def test_host_binding_rejects_claimed_source_and_capability_escalation(self):
        restricted = binding(max_confidence="inferred", offers_text=False,
                             offers_subject=False, can_retract=False)
        candidates = (
            {"source_id": "other.source"},
            {"severity": "critical"},
            {"confidence": "known"},
            {"text": "Synthetic text"},
            {"subject_id": "example-job"},
            {"status": "cancelled", "subject_id": "example-job"},
        )
        for change in candidates:
            data = {**FIXTURE, "confidence": "inferred", "text": None,
                    "subject_id": None, **change}
            frame = parse_frame({"proto": 1, "op": "publish", "event": data})
            with self.subTest(change=change), self.assertRaises(ContractError):
                authorize_frame(restricted, frame)
        other_retract = parse_frame({"proto": 1, "op": "retract",
                                     "source_id": "other.source", "subject_id": "run",
                                     "request_id": "request"})
        with self.assertRaises(ContractError):
            authorize_frame(binding(), other_retract)
        with self.assertRaises(ContractError):
            authorize_frame(restricted, RetractFrame("synthetic.build", "run", "request"))

    def test_binding_fields_are_bounded_host_data(self):
        for change in ({"binding_id": "bad space"},
                       {"source_ids": frozenset()},
                       {"source_ids": frozenset(f"source-{i}" for i in range(17))},
                       {"statuses": frozenset({"invented"})},
                       {"max_severity": "fatal"},
                       {"can_retract": 1}):
            with self.subTest(change=change), self.assertRaises((ValueError, ContractError)):
                binding(**change)


if __name__ == "__main__":
    unittest.main()
