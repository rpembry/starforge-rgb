"""Explicit foreground manual text/audio session; importing has no output effect."""

from datetime import datetime, timezone
from threading import RLock
import uuid

from .audio import AudioAdapter, DEFAULT_GAIN, original_test_earcon
from .contract import CueEvent
from .pipewire_backend import PipeWireBackend
from .text_stack import TextProjectionSink

MANUAL_CUE = "manual.test"
MANUAL_TEXT = "Manual notification test."


class ManualTextSink(TextProjectionSink):
    """Keep only the fixed synthetic test card for this foreground session."""

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._pinned: dict | None = None

    def dispatch(self, channel: str, plan: dict) -> str:
        result = super().dispatch(channel, plan)
        if (plan.get("operation") == "cue" and plan.get("cue_id") == MANUAL_CUE and
                plan.get("source_id") == "manual.local" and plan.get("text") == MANUAL_TEXT):
            occurred = datetime.fromisoformat(plan["occurred_at"]).astimezone(timezone.utc)
            time_label = occurred.strftime("%H:%M UTC")
            with self._lock:
                self._pinned = {"row_id": "manual:pinned", "source_id": "manual.local",
                                "severity": "info", "status": "succeeded", "confidence": "known",
                                "time": time_label, "text": MANUAL_TEXT, "group": None,
                                "count": 1,
                                "label": (f"Info succeeded, source manual.local, known provenance, "
                                          f"{time_label}, {MANUAL_TEXT}")}
        return result

    def pinned_row(self) -> dict | None:
        with self._lock:
            return dict(self._pinned) if self._pinned is not None else None

    def dismiss(self) -> None:
        with self._lock:
            self._pinned = None


def close_foreground(server, audio) -> dict[str, str]:
    """Attempt socket and owned-audio cleanup independently on normal close."""
    result = {"socket": "absent", "audio": "absent"}
    if server is not None:
        try:
            server.shutdown()
            server.close()
            result["socket"] = "closed"
        except Exception:
            result["socket"] = "unknown"
            try:
                server.close()
            except Exception:
                pass
    if audio is not None:
        try:
            result["audio"] = audio.dispatch("audio", {"operation": "clear"})
        except Exception:
            result["audio"] = "unknown"
    return result


def with_manual_card(view: dict, sink: TextProjectionSink,
                     excluded_sources: frozenset[str]) -> dict:
    """Keep the fixed card in this session after the coordinator lease expires."""
    if view["hidden"] or "manual.local" in excluded_sources:
        return view
    pinned = getattr(sink, "pinned_row", lambda: None)()
    if pinned is None or any(row["source_id"] == "manual.local" for row in view["rows"]):
        return view
    return {**view, "rows": [pinned] + view["rows"][:7]}


def manual_event_allowed(event: CueEvent) -> bool:
    """Bound the audio-enabled receiver to the generated manual event shape."""
    return (event.source_id == "manual.local" and event.cue_id == MANUAL_CUE and
            event.confidence == "known" and event.origin_id == "starforge.manual" and
            event.status == "succeeded" and event.severity == "info" and
            event.text == MANUAL_TEXT and event.ttl_ms == 30000 and
            event.subject_id == event.event_id == event.idempotency_key and
            event.correlation_id is None and event.metadata == {} and
            event.occurred_at == event.observed_at)


def manual_sinks(sink_name: str | None = None, *, gain: float = DEFAULT_GAIN,
                 commissioning_override: bool = False, backend=None) -> dict:
    """Construct text plus an optional exact-device audio sink for one cue ID."""
    sinks = {"text": ManualTextSink() if sink_name is not None else TextProjectionSink()}
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
        "severity": "info", "ttl_ms": 30000, "text": MANUAL_TEXT,
    })
