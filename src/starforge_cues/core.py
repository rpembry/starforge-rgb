"""Single portable policy owner and independent output contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from .contract import CueEvent

CHANNELS = ("text", "rgb", "audio")


class Sink(Protocol):
    capabilities: frozenset[str]

    def dispatch(self, channel: str, plan: dict) -> str:
        """Return accepted or unknown; raise on adapter failure. Never claim perception."""


@dataclass(frozen=True)
class SourceCapabilities:
    """Host-registered producer features; producer JSON cannot set these."""

    statuses: frozenset[str]
    offers_text: bool = False
    offers_subject: bool = False

    def supports(self, event: CueEvent) -> bool:
        return (event.status in self.statuses and
                (event.text is None or self.offers_text) and
                (event.subject_id is None or self.offers_subject))


@dataclass
class FakeSink:
    capabilities: frozenset[str] = frozenset(CHANNELS)
    fail: frozenset[str] = frozenset()
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def dispatch(self, channel: str, plan: dict) -> str:
        self.calls.append((channel, plan))
        if channel in self.fail:
            raise RuntimeError("simulated adapter error")
        return "accepted"


@dataclass(frozen=True)
class QuietPolicy:
    quiet: bool = False
    muted: frozenset[str] = frozenset()

    def permits(self, channel: str) -> bool:
        return not self.quiet and channel not in self.muted


class Coordinator:
    def __init__(self, sinks: dict[str, Sink] | None = None,
                 clock: Callable[[], float] | None = None, policy: QuietPolicy = QuietPolicy(),
                 sources: dict[str, SourceCapabilities] | None = None):
        import time
        self.clock = clock or time.time
        self.sinks = sinks or {}
        self.policy = policy
        self.sources = sources
        self._seen: dict[tuple[str, str], float] = {}
        self._seen_events: dict[tuple[str, str], float] = {}
        self._leases: dict[tuple[str, str], tuple[float, int, str]] = {}
        self._rate: dict[str, list[float]] = {}

    def handle(self, event: CueEvent) -> dict:
        if self.sources is not None:
            capabilities = self.sources.get(event.source_id)
            if capabilities is None or not capabilities.supports(event):
                return {"result": "rejected", "reason": "source_capability", "channels": {}}
        now = self.clock()
        expiry = event.occurred_at.timestamp() + event.ttl_ms / 1000
        if event.occurred_at.timestamp() > now + 60 or event.observed_at.timestamp() > now + 60:
            return {"result": "suppressed", "reason": "future", "channels": {}}
        if expiry <= now:
            return {"result": "suppressed", "reason": "stale", "channels": {}}
        for key, end in list(self._seen.items()):
            if end <= now:
                del self._seen[key]
        for key, end in list(self._seen_events.items()):
            if end <= now:
                del self._seen_events[key]
        for source, points in list(self._rate.items()):
            recent_points = [point for point in points if point > now - 60]
            if recent_points:
                self._rate[source] = recent_points
            else:
                del self._rate[source]
        for key, (end, _, _) in list(self._leases.items()):
            if end <= now:
                del self._leases[key]
        key = (event.source_id, event.idempotency_key)
        event_key = (event.source_id, event.event_id)
        if key in self._seen or event_key in self._seen_events:
            return {"result": "suppressed", "reason": "duplicate", "channels": {}}
        if len(self._rate) >= 256 and event.source_id not in self._rate:
            return {"result": "suppressed", "reason": "source_limit", "channels": {}}
        if len(self._seen) >= 4096 or len(self._leases) >= 4096:
            return {"result": "suppressed", "reason": "capacity", "channels": {}}
        recent = [point for point in self._rate.get(event.source_id, []) if point > now - 60]
        if len(recent) >= 10:
            self._rate[event.source_id] = recent
            return {"result": "suppressed", "reason": "rate_limit", "channels": {}}
        recent.append(now)
        self._rate[event.source_id] = recent
        self._seen[key] = expiry
        self._seen_events[event_key] = expiry
        lease_key = (event.source_id, event.subject_id or event.event_id)
        if event.status == "cancelled":
            self._leases.pop(lease_key, None)
        else:
            priority = {"info": 1, "warning": 2, "critical": 3}[event.severity]
            self._leases[lease_key] = (expiry, priority, event.cue_id)
        highest = max((item[1] for item in self._leases.values()), default=0)
        current = sorted((item[2] for item in self._leases.values() if item[1] == highest))
        preempted = event.status != "cancelled" and priority < highest
        # A plan carries meaning, never colors, devices, filenames or commands.
        plan = {"cue_id": event.cue_id, "status": event.status, "severity": event.severity,
                "source_id": event.source_id, "confidence": event.confidence,
                "subject_id": event.subject_id, "text": event.text,
                "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
                "baseline": current[0] if current else None}
        results = {}
        for channel in CHANNELS:
            sink = self.sinks.get(channel)
            if not self.policy.permits(channel):
                results[channel] = "suppressed"
            elif preempted:
                results[channel] = "preempted"
            elif sink is None or channel not in sink.capabilities:
                results[channel] = "unsupported"
            elif channel == "text" and event.text is None:
                results[channel] = "absent"
            else:
                try:
                    outcome = sink.dispatch(channel, dict(plan))
                    results[channel] = outcome if outcome in ("accepted", "unknown") else "unknown"
                except Exception:
                    results[channel] = "failed"
        return {"result": "accepted", "reason": None, "plan": plan, "channels": results}
