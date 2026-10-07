"""Pure proposed ingress types; the current socket does not use them yet."""

from dataclasses import dataclass
from collections import OrderedDict
import math
import json
import re
from typing import Mapping

from .contract import ContractError, CueEvent, MAX_BYTES, _STATUS, _SEVERITY, _identifier


_EPOCH = re.compile(r"[0-9a-f]{32}\Z")
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_CONFIDENCE_RANK = {"unknown": 0, "inferred": 1, "known": 2}
MAX_RETRACT_REQUESTS = 4096
MAX_RETRACT_REQUESTS_PER_BINDING = 256
RETRACT_REPLAY_SECONDS = 600


@dataclass(frozen=True)
class GenerationRef:
    """Host-owned lifetime identity, separate from reusable source/subject names."""

    process_epoch: str
    ordinal: int

    def __post_init__(self):
        if (not isinstance(self.process_epoch, str) or
                not _EPOCH.fullmatch(self.process_epoch) or
                type(self.ordinal) is not int or not 1 <= self.ordinal < 2**63):
            raise ValueError("invalid generation reference")


class GenerationIds:
    """Allocate a bounded process-local ordinal under the host's lock."""

    def __init__(self, process_epoch: str):
        GenerationRef(process_epoch, 1)
        self.process_epoch = process_epoch
        self._last = 0

    def allocate(self) -> GenerationRef:
        if self._last >= 2**63 - 1:
            raise OverflowError("generation ordinal exhausted")
        self._last += 1
        return GenerationRef(self.process_epoch, self._last)


@dataclass(frozen=True)
class AdmittedTransition:
    """Exact host-created admission revision for a future audio decision."""

    generation: GenerationRef
    revision: int
    event_id: str
    status: str
    previous_status: str | None

    def __post_init__(self):
        if (not isinstance(self.generation, GenerationRef) or
                type(self.revision) is not int or not 1 <= self.revision < 2**63 or
                not isinstance(self.status, str) or self.status not in _STATUS or
                (self.previous_status is not None and
                 (not isinstance(self.previous_status, str) or self.previous_status not in _STATUS))):
            raise ValueError("invalid admitted transition")
        _identifier(self.event_id, "event_id")

    def matches(self, generation: GenerationRef, revision: int,
                event_id: str, status: str) -> bool:
        return (self.generation == generation and self.revision == revision and
                self.event_id == event_id and self.status == status)


@dataclass(frozen=True)
class PublishFrame:
    event: CueEvent

    def __post_init__(self):
        if not isinstance(self.event, CueEvent):
            raise ValueError("invalid publish frame")


@dataclass(frozen=True)
class RetractFrame:
    source_id: str
    subject_id: str
    request_id: str
    expected_generation: GenerationRef

    def __post_init__(self):
        for name in ("source_id", "subject_id", "request_id"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.expected_generation, GenerationRef):
            raise ValueError("invalid expected generation")


@dataclass(frozen=True)
class HelloFrame:
    pass


Frame = PublishFrame | RetractFrame | HelloFrame


def parse_frame_json(raw: bytes) -> Frame:
    """Decode one bounded envelope, rejecting duplicate keys at every depth."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ContractError("frame: too large")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("frame: duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("frame: invalid JSON") from exc
    return parse_frame(value)


def parse_frame(value: object) -> Frame:
    """Parse a strict proposed v1 envelope without touching transport."""
    if not isinstance(value, dict) or type(value.get("proto")) is not int or value["proto"] != 1:
        raise ContractError("protocol: unsupported")
    op = value.get("op")
    if op == "publish":
        if value.keys() != {"proto", "op", "event"}:
            raise ContractError("frame: invalid fields")
        return PublishFrame(CueEvent.from_mapping(value["event"]))
    if op == "retract":
        if value.keys() != {"proto", "op", "source_id", "subject_id", "request_id",
                            "expected_generation"}:
            raise ContractError("frame: invalid fields")
        expected = value["expected_generation"]
        if not isinstance(expected, dict) or expected.keys() != {"process_epoch", "ordinal"}:
            raise ContractError("generation: invalid")
        try:
            generation = GenerationRef(expected["process_epoch"], expected["ordinal"])
        except ValueError as exc:
            raise ContractError("generation: invalid") from exc
        return RetractFrame(_identifier(value["source_id"], "source_id"),
                            _identifier(value["subject_id"], "subject_id"),
                            _identifier(value["request_id"], "request_id"), generation)
    if op == "hello":
        if value.keys() != {"proto", "op"}:
            raise ContractError("frame: invalid fields")
        return HelloFrame()
    raise ContractError("frame: unsupported operation")


@dataclass(frozen=True)
class IngressBinding:
    """Host-provided scope; no producer field can create or widen a binding."""

    binding_id: str
    source_ids: frozenset[str]
    statuses: frozenset[str]
    max_severity: str
    max_confidence: str
    offers_text: bool
    offers_subject: bool
    can_retract: bool

    def __post_init__(self):
        _identifier(self.binding_id, "binding_id")
        if (not isinstance(self.source_ids, frozenset) or
                not 1 <= len(self.source_ids) <= 16 or
                not isinstance(self.statuses, frozenset) or
                not 1 <= len(self.statuses) <= len(_STATUS) or
                any(not isinstance(item, str) or item not in _STATUS for item in self.statuses) or
                not isinstance(self.max_severity, str) or self.max_severity not in _SEVERITY or
                not isinstance(self.max_confidence, str) or
                self.max_confidence not in _CONFIDENCE_RANK or
                (self.can_retract and not self.offers_subject) or
                any(type(flag) is not bool for flag in
                    (self.offers_text, self.offers_subject, self.can_retract))):
            raise ValueError("invalid ingress binding")
        for source_id in self.source_ids:
            _identifier(source_id, "source_id")


@dataclass(frozen=True)
class BoundFrame:
    binding_id: str
    frame: Frame

    def __post_init__(self):
        _identifier(self.binding_id, "binding_id")
        if not isinstance(self.frame, (PublishFrame, RetractFrame, HelloFrame)):
            raise ValueError("invalid bound frame")


@dataclass(frozen=True)
class OwnedGeneration:
    binding_id: str
    generation: GenerationRef

    def __post_init__(self):
        _identifier(self.binding_id, "binding_id")
        if not isinstance(self.generation, GenerationRef):
            raise ValueError("invalid owned generation")


def authorize_frame(binding: IngressBinding, frame: Frame) -> BoundFrame:
    """Check static scope only; current generation is checked after replay lookup."""
    if not isinstance(binding, IngressBinding):
        raise TypeError("host binding required")
    if isinstance(frame, PublishFrame):
        event = frame.event
        if (not isinstance(event.source_id, str) or not isinstance(event.status, str) or
                not isinstance(event.severity, str) or event.severity not in _SEVERITY_RANK or
                not isinstance(event.confidence, str) or
                event.confidence not in _CONFIDENCE_RANK or
                event.source_id not in binding.source_ids or
                event.status not in binding.statuses or
                _SEVERITY_RANK[event.severity] > _SEVERITY_RANK[binding.max_severity] or
                _CONFIDENCE_RANK[event.confidence] > _CONFIDENCE_RANK[binding.max_confidence] or
                (event.text is not None and not binding.offers_text) or
                (event.subject_id is not None and not binding.offers_subject) or
                (event.status == "cancelled" and not binding.can_retract)):
            raise ContractError("binding: publish outside scope")
    elif isinstance(frame, RetractFrame):
        if not binding.can_retract or frame.source_id not in binding.source_ids:
            raise ContractError("binding: retract outside scope")
    elif not isinstance(frame, HelloFrame):
        raise ContractError("binding: unsupported frame")
    return BoundFrame(binding.binding_id, frame)


@dataclass(frozen=True, eq=False)
class RetractionReservation:
    """Process-local handle issued only when a new request ID is reserved."""

    binding_id: str
    request_id: str


@dataclass
class _ReplayRecord:
    expiry: float
    frame: RetractFrame
    reservation: RetractionReservation
    completed: bool = False


class RetractionReplay:
    """Bounded binding-scoped request IDs; call under the host admission lock."""

    def __init__(self):
        self._seen: OrderedDict[tuple[str, str], _ReplayRecord] = OrderedDict()
        self._binding_counts: dict[str, int] = {}
        self._last_now = float("-inf")

    def _remove(self, key: tuple[str, str]) -> None:
        del self._seen[key]
        binding_id = key[0]
        remaining = self._binding_counts[binding_id] - 1
        if remaining:
            self._binding_counts[binding_id] = remaining
        else:
            del self._binding_counts[binding_id]

    def _prune(self, now: float) -> None:
        if type(now) not in (int, float) or not math.isfinite(now) or now < self._last_now:
            raise ValueError("retraction replay requires monotonic time")
        self._last_now = now
        while self._seen and next(iter(self._seen.values())).expiry <= now:
            self._remove(next(iter(self._seen)))

    def classify(self, bound: BoundFrame, now: float) -> str:
        if not isinstance(bound, BoundFrame) or not isinstance(bound.frame, RetractFrame):
            raise TypeError("bound retract frame required")
        self._prune(now)
        previous = self._seen.get((bound.binding_id, bound.frame.request_id))
        if previous is not None:
            return "duplicate" if previous.frame == bound.frame else "conflict"
        return ("capacity" if len(self._seen) >= MAX_RETRACT_REQUESTS or
                self._binding_counts.get(bound.binding_id, 0) >= MAX_RETRACT_REQUESTS_PER_BINDING
                else "new")

    def remember(self, bound: BoundFrame, now: float) -> RetractionReservation | str:
        outcome = self.classify(bound, now)
        if outcome == "new":
            reservation = RetractionReservation(bound.binding_id, bound.frame.request_id)
            self._seen[(bound.binding_id, bound.frame.request_id)] = _ReplayRecord(
                now + RETRACT_REPLAY_SECONDS, bound.frame, reservation)
            self._binding_counts[bound.binding_id] = self._binding_counts.get(bound.binding_id, 0) + 1
            return reservation
        return outcome

    def complete(self, reservation: RetractionReservation) -> bool:
        """Commit a successful in-lock retraction; preserve its replay record."""
        if not isinstance(reservation, RetractionReservation):
            return False
        record = self._seen.get((reservation.binding_id, reservation.request_id))
        if record is None or record.reservation is not reservation or record.completed:
            return False
        record.completed = True
        return True

    def rollback(self, reservation: RetractionReservation) -> bool:
        """Only the exact pending new-request handle may remove its record."""
        if not isinstance(reservation, RetractionReservation):
            return False
        key = (reservation.binding_id, reservation.request_id)
        record = self._seen.get(key)
        if record is None or record.reservation is not reservation or record.completed:
            return False
        self._remove(key)
        return True


def admit_retraction(binding: IngressBinding, frame: RetractFrame,
                     owned_generations: Mapping[tuple[str, str], OwnedGeneration],
                     replay: RetractionReplay, now: float) -> RetractionReservation | str:
    """Classify and reserve one retract under the host lock; do not execute it.

    A recorded exact retry is acknowledged before current ownership is checked,
    since a successful first retract already removed that ownership. New requests
    must still prove ownership of the exact expected generation.
    """
    bound = authorize_frame(binding, frame)
    if (not isinstance(frame, RetractFrame) or not isinstance(replay, RetractionReplay) or
            not isinstance(owned_generations, Mapping)):
        raise TypeError("retract frame, ownership and replay window required")
    prior = replay.classify(bound, now)
    if prior == "duplicate":
        return "duplicate"
    if prior == "conflict":
        raise ContractError("retract: replay_conflict")
    if prior == "capacity":
        raise ContractError("retract: capacity")
    owner = owned_generations.get((frame.source_id, frame.subject_id))
    if (not isinstance(owner, OwnedGeneration) or
            owner.binding_id != binding.binding_id or
            owner.generation != frame.expected_generation):
        raise ContractError("retract: generation mismatch")
    return replay.remember(bound, now)
