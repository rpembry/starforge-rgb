"""Single portable policy owner and independent output contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
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
    state: dict[str, dict | None] = field(default_factory=dict)

    def dispatch(self, channel: str, plan: dict) -> str:
        self.calls.append((channel, dict(plan)))
        if channel in self.fail:
            raise RuntimeError("simulated adapter error")
        self.state[channel] = None if plan["operation"] == "clear" else dict(plan)
        return "accepted"


@dataclass(frozen=True)
class QuietPolicy:
    quiet: bool = False
    muted: frozenset[str] = frozenset()

    def permits(self, channel: str) -> bool:
        return not self.quiet and channel not in self.muted


@dataclass(frozen=True)
class _Lease:
    expiry: float
    priority: int
    sequence: int
    plan: dict


class Coordinator:
    """In-memory policy; a host timer calls tick even when no event arrives."""

    def __init__(self, sinks: dict[str, Sink] | None = None,
                 clock: Callable[[], float] | None = None, policy: QuietPolicy = QuietPolicy(),
                 sources: dict[str, SourceCapabilities] | None = None):
        import time
        self.clock = clock or time.time
        self.sinks = sinks or {}
        self.policy = policy
        self.sources = sources
        self._lock = RLock()
        self._seen: dict[tuple[str, str], tuple[float, CueEvent]] = {}
        self._seen_events: dict[tuple[str, str], tuple[str, str]] = {}
        self._leases: dict[tuple[str, str], _Lease] = {}
        self._rate: dict[str, list[float]] = {}
        self._active_key: tuple[str, str] | None = None
        self._sequence = 0

    def _prune(self, now: float) -> None:
        for key, (end, event) in list(self._seen.items()):
            if end <= now:
                del self._seen[key]
                self._seen_events.pop((event.source_id, event.event_id), None)
        for source, points in list(self._rate.items()):
            recent = [point for point in points if point > now - 60]
            if recent:
                self._rate[source] = recent
            else:
                del self._rate[source]
        for key, lease in list(self._leases.items()):
            if lease.expiry <= now:
                del self._leases[key]

    def _top(self) -> tuple[str, str] | None:
        if not self._leases:
            return None
        return max(self._leases, key=lambda key: (self._leases[key].priority,
                                                  self._leases[key].sequence))

    def _dispatch(self, plan: dict) -> dict[str, str]:
        results = {}
        for channel in CHANNELS:
            sink = self.sinks.get(channel)
            if plan["operation"] != "clear" and not self.policy.permits(channel):
                results[channel] = "suppressed"
            elif sink is None or channel not in sink.capabilities:
                results[channel] = "unsupported"
            elif channel == "text" and plan["operation"] != "clear" and plan["text"] is None:
                results[channel] = "absent"
            else:
                try:
                    outcome = sink.dispatch(channel, dict(plan))
                    results[channel] = outcome if outcome in ("accepted", "unknown") else "unknown"
                except Exception:
                    results[channel] = "failed"
        return results

    def _reconcile(self) -> dict:
        top = self._top()
        if top == self._active_key:
            return {"result": "unchanged", "channels": {}}
        self._active_key = top
        if top is None:
            plan = {"operation": "clear", "baseline": None, "text": None}
        else:
            plan = {**self._leases[top].plan, "operation": "restore",
                    "baseline": self._leases[top].plan["cue_id"]}
        return {"result": "reconciled", "plan": plan, "channels": self._dispatch(plan)}

    def tick(self) -> dict:
        """Expire leases and dispatch a clear or restored baseline, without new input."""
        with self._lock:
            self._prune(self.clock())
            return self._reconcile()

    def _remember(self, key: tuple[str, str], event_key: tuple[str, str],
                  expiry: float, event: CueEvent) -> None:
        if len(self._seen) >= 4096:
            # Reserve admission for cancellation by evicting the oldest replay record.
            old_key = next(iter(self._seen))
            old_event = self._seen.pop(old_key)[1]
            self._seen_events.pop((old_event.source_id, old_event.event_id), None)
        self._seen[key] = (expiry, event)
        self._seen_events[event_key] = key

    def handle(self, event: CueEvent) -> dict:
        with self._lock:
            return self._handle(event)

    def _handle(self, event: CueEvent) -> dict:
        if self.sources is not None:
            capabilities = self.sources.get(event.source_id)
            if capabilities is None or not capabilities.supports(event):
                return {"result": "rejected", "reason": "source_capability", "channels": {}}
        now = self.clock()
        self._prune(now)
        self._reconcile()
        expiry = event.occurred_at.timestamp() + event.ttl_ms / 1000
        if event.occurred_at.timestamp() > now + 60 or event.observed_at.timestamp() > now + 60:
            return {"result": "suppressed", "reason": "future", "channels": {}}
        if expiry <= now:
            return {"result": "suppressed", "reason": "stale", "channels": {}}
        key = (event.source_id, event.idempotency_key)
        event_key = (event.source_id, event.event_id)
        prior = self._seen.get(key)
        if prior is None and event_key in self._seen_events:
            prior = self._seen.get(self._seen_events[event_key])
        if prior is not None:
            if prior[1] == event:
                return {"result": "suppressed", "reason": "duplicate", "channels": {}}
            return {"result": "rejected", "reason": "replay_conflict", "channels": {}}
        lease_key = (event.source_id, event.subject_id or event.event_id)
        cancellation = event.status == "cancelled"
        if not cancellation:
            if len(self._rate) >= 256 and event.source_id not in self._rate:
                return {"result": "suppressed", "reason": "source_limit", "channels": {}}
            if len(self._seen) >= 4096 or (len(self._leases) >= 4096 and lease_key not in self._leases):
                return {"result": "suppressed", "reason": "capacity", "channels": {}}
            recent = self._rate.get(event.source_id, [])
            if len(recent) >= 10:
                return {"result": "suppressed", "reason": "rate_limit", "channels": {}}
            self._rate[event.source_id] = recent + [now]
        self._remember(key, event_key, expiry, event)
        if cancellation:
            self._leases.pop(lease_key, None)
            reconciliation = self._reconcile()
            return {"result": "accepted", "reason": None, "plan": reconciliation.get("plan"),
                    "channels": reconciliation["channels"]}
        self._sequence += 1
        plan = {"operation": "cue", "cue_id": event.cue_id, "status": event.status,
                "severity": event.severity, "source_id": event.source_id,
                "confidence": event.confidence, "subject_id": event.subject_id,
                "text": event.text,
                "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
                "baseline": event.cue_id}
        self._leases[lease_key] = _Lease(expiry, {"info": 1, "warning": 2,
                                                 "critical": 3}[event.severity], self._sequence, plan)
        if self._top() != lease_key:
            return {"result": "accepted", "reason": None, "plan": plan,
                    "channels": {channel: "preempted" for channel in CHANNELS}}
        self._active_key = lease_key
        return {"result": "accepted", "reason": None, "plan": plan,
                "channels": self._dispatch(plan)}
