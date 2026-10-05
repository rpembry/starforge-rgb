"""Offline P1S status normalization boundary; no printer or network access."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import time
from typing import Callable

from .contract import CueEvent

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_STATES = frozenset({"printing", "paused", "finished", "failed", "idle", "unknown"})


@dataclass(frozen=True)
class PrinterObservation:
    """A host collector's *normalized* report, not raw Bambu MQTT or a command."""

    connection_epoch: int
    sequence: int
    observed_at: datetime
    state: str
    job_id: str | None

    def __post_init__(self):
        if (type(self.connection_epoch) is not int or
                not 1 <= self.connection_epoch <= 2**31 - 1 or
                type(self.sequence) is not int or not 0 <= self.sequence <= 2**31 - 1):
            raise ValueError("invalid collector ordering")
        if (not isinstance(self.observed_at, datetime) or
                self.observed_at.tzinfo is None or
                self.observed_at.utcoffset() is None or
                self.observed_at.utcoffset().total_seconds() != 0):
            raise ValueError("collector timestamp must be UTC")
        if not isinstance(self.state, str) or self.state not in _STATES:
            raise ValueError("invalid printer state")
        if self.state in {"printing", "paused", "finished", "failed"}:
            if not isinstance(self.job_id, str) or not _ID.fullmatch(self.job_id):
                raise ValueError("active or terminal state requires opaque job ID")
        elif self.job_id is not None:
            raise ValueError("idle or unknown state cannot assert a job ID")


@dataclass(frozen=True)
class PrinterDecision:
    outcome: str  # emitted or suppressed
    reason: str
    event: CueEvent | None = None


class BambuCompletionAdapter:
    """Emit only a fresh explicit terminal transition after observed printing.

    A future authorized collector supplies observations and monotonically
    increasing connection epochs. This class performs no I/O and cannot send a
    printer command. It does not infer failure from silence or disconnection.
    """

    def __init__(self, source_id: str, *, clock: Callable[[], float] = time.time,
                 max_age_s: float = 20):
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
            raise ValueError("invalid host-owned printer source")
        if type(max_age_s) not in (int, float) or not 1 <= max_age_s <= 25:
            raise ValueError("invalid freshness bound")
        self.source_id = source_id
        self.clock = clock
        self.max_age_s = max_age_s
        self._epoch = 0
        self._sequence = -1
        self._last_observed = float("-inf")
        self._active_job: str | None = None
        self._terminal: OrderedDict[str, None] = OrderedDict()

    def observe(self, item: PrinterObservation) -> PrinterDecision:
        if not isinstance(item, PrinterObservation):
            raise TypeError("expected normalized printer observation")
        if item.connection_epoch < self._epoch:
            return PrinterDecision("suppressed", "old_connection")
        if item.connection_epoch > self._epoch:
            self._epoch = item.connection_epoch
            self._sequence = -1
            self._last_observed = float("-inf")
            self._active_job = None  # reconnect cannot prove an unseen ending
        observed = item.observed_at.timestamp()
        age = self.clock() - observed
        if not -5 <= age <= self.max_age_s:
            self._active_job = None  # a freshness gap breaks transition proof
            return PrinterDecision("suppressed", "stale_or_future")
        if item.sequence <= self._sequence or observed < self._last_observed:
            return PrinterDecision("suppressed", "out_of_order")
        self._sequence = item.sequence
        self._last_observed = observed

        if item.state in {"unknown", "idle"}:
            self._active_job = None
            return PrinterDecision("suppressed", item.state)
        if item.state == "printing":
            if item.job_id not in self._terminal:
                self._active_job = item.job_id
            return PrinterDecision("suppressed", "active")
        if item.state == "paused":
            if item.job_id != self._active_job:
                self._active_job = None
            return PrinterDecision("suppressed", "paused")
        # A terminal snapshot alone, or one after reconnect/unknown, proves no
        # transition. Require this exact job to have been seen printing here.
        if item.job_id in self._terminal:
            return PrinterDecision("suppressed", "duplicate_terminal")
        if item.job_id != self._active_job:
            return PrinterDecision("suppressed", "unproven_terminal")
        self._active_job = None
        self._terminal[item.job_id] = None
        if len(self._terminal) > 256:
            self._terminal.popitem(last=False)
        job_digest = hashlib.sha256(f"{self.source_id}\0{item.job_id}".encode()).hexdigest()[:32]
        terminal = "completed" if item.state == "finished" else "failed"
        event_id = f"p{job_digest}{'c' if terminal == 'completed' else 'f'}"
        stamp = item.observed_at.astimezone(timezone.utc).isoformat()
        event = CueEvent.from_mapping({
            "version": 1, "event_id": event_id, "idempotency_key": event_id,
            "cue_id": f"printer.{terminal}", "source_id": self.source_id,
            "confidence": "known", "subject_id": f"j{job_digest}",
            "origin_id": "starforge.printer", "occurred_at": stamp,
            "observed_at": stamp,
            "status": "succeeded" if terminal == "completed" else "failed",
            "severity": "info" if terminal == "completed" else "warning",
            "ttl_ms": 30000,
            "text": "Print finished." if terminal == "completed" else "Print failed.",
        })
        return PrinterDecision("emitted", terminal, event)
