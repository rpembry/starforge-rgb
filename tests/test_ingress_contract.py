"""Proposed binding and frame types use synthetic data only; no listener."""

import json
from pathlib import Path
import unittest

from starforge_cues.contract import ContractError
from starforge_cues.ingress_contract import (
    AdmittedTransition, BoundFrame, GenerationIds, GenerationRef, HelloFrame,
    IngressBinding, OwnedGeneration, PublishFrame, RetractFrame, RetractionReplay,
    RetractionReservation,
    admit_retraction, authorize_frame, parse_frame,
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
        self.assertEqual(authorize_frame(binding(), retract).frame, retract)
        self.assertIsInstance(admit_retraction(binding(), retract, owned,
                                                RetractionReplay(), 0), RetractionReservation)
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
            admit_retraction(binding(), old, ownership, RetractionReplay(), 0)
        self.assertIsInstance(admit_retraction(binding(), current, ownership,
                                                RetractionReplay(), 0), RetractionReservation)
        with self.assertRaises(ContractError):
            admit_retraction(binding(binding_id="other.binding"), current, ownership,
                             RetractionReplay(), 0)
        with self.assertRaises(ContractError):
            admit_retraction(binding(), current, {}, RetractionReplay(), 0)

    def test_successful_retract_lost_ack_then_exact_retry_is_idempotent(self):
        key = ("synthetic.build", "reused-name")
        first = GenerationRef(EPOCH, 1)
        request = RetractFrame(*key, "request-one", first)
        ownership = {key: OwnedGeneration("synthetic.binding", first)}
        replay = RetractionReplay()
        reservation = admit_retraction(binding(), request, ownership, replay, 0)
        self.assertIsInstance(reservation, RetractionReservation)
        del ownership[key]  # successful retraction removed the original lease
        self.assertTrue(replay.complete(reservation))
        duplicate = admit_retraction(binding(), request, ownership, replay, 1)
        self.assertEqual(duplicate, "duplicate")
        self.assertFalse(replay.rollback(duplicate))
        self.assertFalse(replay.rollback(reservation))
        self.assertEqual(replay.classify(BoundFrame("synthetic.binding", request), 1), "duplicate")
        ownership[key] = OwnedGeneration("synthetic.binding", GenerationRef(EPOCH, 2))
        self.assertEqual(admit_retraction(binding(), request, ownership, replay, 2), "duplicate")
        changed = RetractFrame(*key, "request-one", GenerationRef(EPOCH, 2))
        with self.assertRaisesRegex(ContractError, "replay_conflict"):
            admit_retraction(binding(), changed, ownership, replay, 2)
        self.assertEqual(ownership[key].generation, GenerationRef(EPOCH, 2))

    def test_failed_retract_reservation_can_roll_back_before_unlock(self):
        key = ("synthetic.build", "run")
        gen = GenerationRef(EPOCH, 1)
        request = RetractFrame(*key, "retry-after-failure", gen)
        ownership = {key: OwnedGeneration("synthetic.binding", gen)}
        replay = RetractionReplay()
        old_reservation = admit_retraction(binding(), request, ownership, replay, 0)
        self.assertIsInstance(old_reservation, RetractionReservation)
        self.assertTrue(replay.rollback(old_reservation))
        new_reservation = admit_retraction(binding(), request, ownership, replay, 1)
        self.assertIsInstance(new_reservation, RetractionReservation)
        self.assertIsNot(old_reservation, new_reservation)
        self.assertFalse(replay.rollback(old_reservation))
        forged = RetractionReservation("synthetic.binding", "retry-after-failure")
        self.assertFalse(replay.rollback(forged))
        self.assertEqual(replay.classify(BoundFrame("synthetic.binding", request), 1), "duplicate")
        self.assertTrue(replay.complete(new_reservation))
        self.assertFalse(replay.rollback(new_reservation))

    def test_retract_request_ids_are_bounded_and_binding_scoped(self):
        replay = RetractionReplay()
        gen = GenerationRef(EPOCH, 1)
        first = BoundFrame("binding.one", RetractFrame("synthetic.build", "run", "id-1", gen))
        self.assertIsInstance(replay.remember(first, 0), RetractionReservation)
        self.assertEqual(replay.classify(first, 0), "duplicate")
        changed = BoundFrame("binding.one", RetractFrame("synthetic.build", "other", "id-1", gen))
        self.assertEqual(replay.classify(changed, 0), "conflict")
        other_binding = BoundFrame("binding.two", first.frame)
        self.assertIsInstance(replay.remember(other_binding, 0), RetractionReservation)
        for index in range(2, 257):
            frame = BoundFrame("binding.one", RetractFrame("synthetic.build", "run",
                                                            f"id-{index}", gen))
            self.assertIsInstance(replay.remember(frame, 0), RetractionReservation)
        per_binding_overflow = BoundFrame("binding.one", RetractFrame(
            "synthetic.build", "run", "overflow", gen))
        self.assertEqual(replay.remember(per_binding_overflow, 0), "capacity")
        for binding_index in range(2, 17):
            name = f"binding.{binding_index}"
            for index in range(2 if binding_index == 2 else 1, 257):
                frame = BoundFrame(name, RetractFrame("synthetic.build", "run",
                                                     f"id-{index}", gen))
                self.assertIsInstance(replay.remember(frame, 0), RetractionReservation)
        global_overflow = BoundFrame("binding.seventeen", RetractFrame(
            "synthetic.build", "run", "overflow", gen))
        self.assertEqual(replay.remember(global_overflow, 0), "capacity")
        self.assertIsInstance(replay.remember(global_overflow, 601), RetractionReservation)

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
