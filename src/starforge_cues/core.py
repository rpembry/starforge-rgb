"""Single portable policy owner and independent output contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
import re
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
class QuietWindow:
    """Local minutes after midnight; end is exclusive, equal endpoints mean all day."""

    start_minute: int
    end_minute: int

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value < 1440
               for value in (self.start_minute, self.end_minute)):
            raise ValueError("quiet window minutes must be within 0..1439")

    def active(self, minute: int) -> bool:
        start, end = self.start_minute, self.end_minute
        return (start == end or
                (start <= minute < end if start < end else minute >= start or minute < end))


@dataclass(frozen=True)
class QuietPolicy:
    quiet: bool = False
    muted: frozenset[str] = frozenset()
    dnd: bool = False
    windows: tuple[QuietWindow, ...] = ()
    local_timezone: tzinfo = timezone.utc

    def permits(self, channel: str, at: float | None = None) -> bool:
        if self.quiet or self.dnd or channel in self.muted:
            return False
        if self.windows and at is not None:
            local = datetime.fromtimestamp(at, self.local_timezone)
            minute = local.hour * 60 + local.minute
            if any(window.active(minute) for window in self.windows):
                return False
        return True


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
                 sources: dict[str, SourceCapabilities] | None = None,
                 own_origins: frozenset[str] = frozenset(), cooldown_seconds: float = 0):
        import time
        self.clock = clock or time.time
        self.sinks = sinks or {}
        self.policy = policy
        self.sources = sources
        self.own_origins = own_origins
        if not 0 <= cooldown_seconds <= 60:
            raise ValueError("cooldown must be within 0..60 seconds")
        self.cooldown_seconds = cooldown_seconds
        self._lock = RLock()
        self._seen: dict[tuple[str, str], tuple[float, CueEvent]] = {}
        self._seen_events: dict[tuple[str, str], tuple[str, str]] = {}
        self._leases: dict[tuple[str, str], _Lease] = {}
        self._rate: dict[str, list[float]] = {}
        self._active_key: tuple[str, str] | None = None
        self._sequence = 0
        self._baseline_generation = 0
        self._baseline_plan: dict | None = None
        self._emitted_baseline_generation = -1
        self._last_permitted = {channel: True for channel in CHANNELS}
        self._desired: dict[str, dict] = {}
        self._applied: dict[str, dict | None] = {}
        self._pending: dict[str, tuple[dict, int]] = {}

    def set_baseline(self, generation: int, cue_id: str | None, text: str | None = None) -> dict:
        """Host-owned baseline update; an active lease continues until it ends."""
        with self._lock:
            self._prune(self.clock())
            if type(generation) is not int or generation <= self._baseline_generation:
                raise ValueError("baseline generation must increase")
            if cue_id is not None and (not isinstance(cue_id, str) or
                                       not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", cue_id)):
                raise ValueError("invalid baseline cue")
            if text is not None and (not isinstance(text, str) or len(text) > 280 or
                                     any(ord(char) < 32 and char not in "\n\t" for char in text)):
                raise ValueError("invalid baseline text")
            self._baseline_generation = generation
            self._baseline_plan = None if cue_id is None else {
                "operation": "restore", "cue_id": cue_id, "status": "baseline",
                "severity": "info", "source_id": "local.baseline", "confidence": "known",
                "subject_id": None, "text": text, "expires_at": None,
                "baseline": cue_id, "baseline_generation": generation}
            return self._reconcile()

    def set_policy(self, policy: QuietPolicy) -> dict:
        with self._lock:
            self._prune(self.clock())
            self.policy = policy
            state = self._reconcile()
            changed = self._sync_policy()
            return state if state["result"] != "unchanged" else changed

    def _sync_policy(self) -> dict:
        """Clear newly muted outputs; restore visible state when allowed again."""
        results = {}
        now = self.clock()
        for channel in CHANNELS:
            permitted = self.policy.permits(channel, now)
            before = self._last_permitted[channel]
            self._last_permitted[channel] = permitted
            if before and not permitted and self._applied.get(channel) is not None:
                results[channel] = self._send(channel, {"operation": "clear", "baseline": None, "text": None})
            elif not before and permitted and channel != "audio":
                desired = self._desired.get(channel)
                if desired and desired["operation"] != "clear":
                    results[channel] = self._send(channel, {**desired, "operation": "restore"})
        return {"result": "reconciled" if results else "unchanged", "channels": results}

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

    def _send(self, channel: str, plan: dict, prior_attempts: int = 0) -> str:
        sink = self.sinks.get(channel)
        if plan["operation"] != "clear" and not self.policy.permits(channel, self.clock()):
            self._pending.pop(channel, None)
            return "suppressed"
        if sink is None or channel not in sink.capabilities:
            self._pending.pop(channel, None)
            return "unsupported"
        try:
            outcome = sink.dispatch(channel, dict(plan))
        except Exception:
            # A cue or restore sound may have played before an adapter error.
            # Only a clear is safe to retry on the audio channel.
            retryable = channel != "audio" or plan["operation"] == "clear"
            if retryable and prior_attempts + 1 < 3:
                self._pending[channel] = (plan, prior_attempts + 1)
            else:
                self._pending.pop(channel, None)
            return "failed"
        self._pending.pop(channel, None)
        if outcome == "accepted":
            self._applied[channel] = None if plan["operation"] == "clear" else plan
            return "accepted"
        # An ambiguous receipt must not be retried automatically.
        return "unknown"

    def _dispatch(self, plan: dict) -> dict[str, str]:
        results = {}
        for channel in CHANNELS:
            effective = plan
            if channel == "text" and plan["operation"] != "clear" and plan["text"] is None:
                effective = {"operation": "clear", "baseline": None, "text": None}
                self._desired[channel] = effective
                if self._applied.get(channel) is None and channel not in self._pending:
                    results[channel] = "absent"
                    continue
            else:
                self._desired[channel] = effective
            results[channel] = self._send(channel, effective)
        return results

    def _retry_pending(self) -> dict:
        results = {}
        for channel, (plan, attempts) in list(self._pending.items()):
            if self._desired.get(channel) == plan:
                results[channel] = self._send(channel, plan, attempts)
        return {"result": "reconciled" if results else "unchanged", "channels": results}

    def _reconcile(self) -> dict:
        top = self._top()
        if (top == self._active_key and
                (top is not None or self._emitted_baseline_generation == self._baseline_generation)):
            return self._retry_pending()
        self._active_key = top
        if top is None:
            self._emitted_baseline_generation = self._baseline_generation
            plan = self._baseline_plan or {"operation": "clear", "baseline": None, "text": None}
        else:
            plan = {**self._leases[top].plan, "operation": "restore",
                    "baseline": self._leases[top].plan["cue_id"]}
        return {"result": "reconciled", "plan": plan, "channels": self._dispatch(plan)}

    def tick(self) -> dict:
        """Expire leases and dispatch a clear or restored baseline, without new input."""
        with self._lock:
            self._prune(self.clock())
            state = self._reconcile()
            policy = self._sync_policy()
            return state if state["result"] != "unchanged" else policy

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
        if event.origin_id in self.own_origins:
            return {"result": "suppressed", "reason": "self_origin", "channels": {}}
        if self.sources is not None:
            capabilities = self.sources.get(event.source_id)
            if capabilities is None or not capabilities.supports(event):
                return {"result": "rejected", "reason": "source_capability", "channels": {}}
        now = self.clock()
        self._prune(now)
        self._reconcile()
        self._sync_policy()
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
            if self.cooldown_seconds and recent and now - recent[-1] < self.cooldown_seconds:
                return {"result": "suppressed", "reason": "cooldown", "channels": {}}
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
