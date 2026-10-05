"""Explicit foreground manual text/audio session; importing has no output effect."""

from datetime import datetime, timezone
import uuid

from .audio import AudioAdapter, DEFAULT_GAIN, original_test_earcon
from .contract import CueEvent
from .pipewire_backend import PipeWireBackend
from .text_stack import TextProjectionSink

MANUAL_CUE = "manual.test"


def manual_sinks(sink_name: str | None = None, *, gain: float = DEFAULT_GAIN,
                 commissioning_override: bool = False, backend=None) -> dict:
    """Construct text plus an optional exact-device audio sink for one cue ID."""
    sinks = {"text": TextProjectionSink()}
    if sink_name is None:
        if backend is not None or commissioning_override or gain != DEFAULT_GAIN:
            raise ValueError("audio options require a selected sink")
        return sinks
    selected = backend if backend is not None else PipeWireBackend(
        commissioning_override=commissioning_override)
    sinks["audio"] = AudioAdapter(selected, sink_name, {MANUAL_CUE: original_test_earcon()},
                                  gain=gain, commissioning_override=commissioning_override)
    return sinks


def manual_event(*, instant: datetime | None = None, event_id: str | None = None) -> CueEvent:
    """Fresh synthetic event with no private contents or producer integration."""
    instant = instant or datetime.now(timezone.utc)
    identifier = event_id or uuid.uuid4().hex
    stamp = instant.isoformat()
    return CueEvent.from_mapping({
        "version": 1, "event_id": identifier, "idempotency_key": identifier,
        "cue_id": MANUAL_CUE, "source_id": "manual.local", "confidence": "known",
        "subject_id": identifier, "origin_id": "starforge.manual",
        "occurred_at": stamp, "observed_at": stamp, "status": "succeeded",
        "severity": "info", "ttl_ms": 30000, "text": "Manual notification test.",
    })
