"""Proposed binding and frame types use synthetic data only; no listener."""

import json
from pathlib import Path
import unittest

from starforge_cues.contract import ContractError
from starforge_cues.ingress_contract import (
    AdmittedTransition, BoundFrame, GenerationIds, GenerationRef, HelloFrame,
    IngressBinding, OwnedGeneration, PublishFrame, RetractFrame, RetractionReplay,
    authorize_frame, parse_frame,
)


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
EPOCH = "0123456789abcdef0123456789abcdef"
GENERATION = {"process_epoch": EPOCH, "ordinal": 1}


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
                               "subject_id": "example-job", "request_id": "retract-one",
                               "expected_generation": GENERATION})
        hello = parse_frame({"proto": 1, "op": "hello"})
        self.assertIsInstance(publish, PublishFrame)
        self.assertIsInstance(retract, RetractFrame)
        self.assertIsInstance(hello, HelloFrame)
        self.assertEqual(authorize_frame(binding(), publish).binding_id, "synthetic.binding")
        owned = {("synthetic.build", "example-job"):
                 OwnedGeneration("synthetic.binding", GenerationRef(EPOCH, 1))}
        self.assertEqual(authorize_frame(binding(), retract, owned).frame, retract)
        self.assertEqual(authorize_frame(binding(), hello).frame, hello)
        for frame in ({"proto": True, "op": "hello"}, {"proto": 2, "op": "hello"},
                      {"proto": 1, "op": "hello", "token": "no"},
                      {"proto": 1, "op": "retract", "source_id": "synthetic.build"},
                      {"proto": 1, "op": "retract", "source_id": "synthetic.build",
                       "subject_id": "example-job", "request_id": "missing-generation"},
                      {"proto": 1, "op": "retract", "source_id": "synthetic.build",
                       "subject_id": "example-job", "request_id": "bad-generation",
                       "expected_generation": {"process_epoch": EPOCH, "ordinal": True}},
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
                                     "request_id": "request",
                                     "expected_generation": GENERATION})
        with self.assertRaises(ContractError):
            authorize_frame(binding(), other_retract)
        with self.assertRaises(ContractError):
            authorize_frame(restricted, RetractFrame("synthetic.build", "run", "request",
                                                     GenerationRef(EPOCH, 1)))

    def test_old_generation_retract_cannot_target_reused_subject(self):
        first, second = GenerationIds(EPOCH).allocate(), GenerationRef(EPOCH, 2)
        key = ("synthetic.build", "same-name")
        old = RetractFrame(*key, "old-request", first)
        current = RetractFrame(*key, "new-request", second)
        ownership = {key: OwnedGeneration("synthetic.binding", second)}
        with self.assertRaises(ContractError):
            authorize_frame(binding(), old, ownership)
        self.assertEqual(authorize_frame(binding(), current, ownership).frame, current)
        with self.assertRaises(ContractError):
            authorize_frame(binding(binding_id="other.binding"), current, ownership)
        with self.assertRaises(ContractError):
            authorize_frame(binding(), current)

    def test_retract_request_ids_are_bounded_and_binding_scoped(self):
        replay = RetractionReplay()
        gen = GenerationRef(EPOCH, 1)
        first = BoundFrame("binding.one", RetractFrame("synthetic.build", "run", "id-1", gen))
        self.assertEqual(replay.remember(first, 0), "new")
        self.assertEqual(replay.classify(first, 0), "duplicate")
        changed = BoundFrame("binding.one", RetractFrame("synthetic.build", "other", "id-1", gen))
        self.assertEqual(replay.classify(changed, 0), "conflict")
        other_binding = BoundFrame("binding.two", first.frame)
        self.assertEqual(replay.remember(other_binding, 0), "new")
        for index in range(2, 257):
            frame = BoundFrame("binding.one", RetractFrame("synthetic.build", "run",
                                                            f"id-{index}", gen))
            self.assertEqual(replay.remember(frame, 0), "new")
        per_binding_overflow = BoundFrame("binding.one", RetractFrame(
            "synthetic.build", "run", "overflow", gen))
        self.assertEqual(replay.remember(per_binding_overflow, 0), "capacity")
        for binding_index in range(2, 17):
            name = f"binding.{binding_index}"
            for index in range(2 if binding_index == 2 else 1, 257):
                frame = BoundFrame(name, RetractFrame("synthetic.build", "run",
                                                     f"id-{index}", gen))
                self.assertEqual(replay.remember(frame, 0), "new")
        global_overflow = BoundFrame("binding.seventeen", RetractFrame(
            "synthetic.build", "run", "overflow", gen))
        self.assertEqual(replay.remember(global_overflow, 0), "capacity")
        self.assertEqual(replay.remember(global_overflow, 601), "new")

    def test_binding_fields_are_bounded_host_data(self):
        for change in ({"binding_id": "bad space"},
                       {"source_ids": frozenset()},
                       {"source_ids": frozenset(f"source-{i}" for i in range(17))},
                       {"statuses": frozenset({"invented"})},
                       {"max_severity": "fatal"},
                       {"offers_subject": False, "can_retract": True},
                       {"can_retract": 1}):
            with self.subTest(change=change), self.assertRaises((ValueError, ContractError)):
                binding(**change)


if __name__ == "__main__":
    unittest.main()
