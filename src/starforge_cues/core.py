"""Single portable policy owner and independent output contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
import re
from threading import RLock
from typing import Callable, Protocol

from .contract import CueEvent

CHANNELS = ("text", "rgb", "audio")
MAX_SUBJECT_GENERATIONS = 2048
MAX_REPLAY_RECORDS = 4096
TERMINAL_REPLAY_RESERVE = 512
TERMINAL_RATE_PER_SOURCE = 20
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


def _valid_baseline_text(text: str | None) -> bool:
    return (text is None or
            (isinstance(text, str) and len(text) <= 280 and
             all(char.isprintable() or char in "\n\t" for char in text)))


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
    first_seen: float
    renewals: int
    priority: int
    sequence: int
    plan: dict


class Coordinator:
    """In-memory policy; a host timer calls tick even when no event arrives."""

    def __init__(self, sinks: dict[str, Sink] | None = None,
                 clock: Callable[[], float] | None = None, policy: QuietPolicy = QuietPolicy(),
                 sources: dict[str, SourceCapabilities] | None = None,
                 own_origins: frozenset[str] = frozenset(), cooldown_seconds: float = 0,
                 max_lease_age_s: float = 3600,
                 monotonic_clock: Callable[[], float] | None = None):
        import time
        self.clock = clock or time.time
        self.monotonic_clock = monotonic_clock or (clock if clock is not None else time.monotonic)
        self.sinks = sinks or {}
        self.policy = policy
        self.sources = sources
        self.own_origins = own_origins
        if not 0 <= cooldown_seconds <= 60:
            raise ValueError("cooldown must be within 0..60 seconds")
        self.cooldown_seconds = cooldown_seconds
        if type(max_lease_age_s) not in (int, float) or not 300 <= max_lease_age_s <= 86400:
            raise ValueError("maximum lease age must be within 300..86400 seconds")
        self.max_lease_age_s = max_lease_age_s
        self._lock = RLock()
        self._seen: dict[tuple[str, str], tuple[float, CueEvent]] = {}
        self._seen_events: dict[tuple[str, str], tuple[str, str]] = {}
        self._leases: dict[tuple[str, str], _Lease] = {}
        self._rate: dict[str, list[float]] = {}
        self._terminal_rate: dict[str, list[float]] = {}
        self._active_key: tuple[str, str] | None = None
        self._sequence = 0
        self._baseline_generation = 0
        self._allow_initial_host_generation = False
        self._baseline_plan: dict | None = None
        self._emitted_baseline_generation = -1
        # Recovery may load a quiet policy before the first timer tick. Track its
        # actual initial state so unquiet cannot replay baseline audio as a cue.
        self._last_permitted = {channel: policy.permits(channel, self.clock())
                                for channel in CHANNELS}
        self._desired: dict[str, dict] = {}
        self._applied: dict[str, dict | None] = {}
        self._pending: dict[str, tuple[dict, int]] = {}
        self._feedback: dict[tuple[str, str], float] = {}
        self._retired_subjects: dict[tuple[str, str], float] = {}
        self.recovery_result: dict | None = None

    @classmethod
    def recover_baseline(cls, generation: int, cue_id: str | None, text: str | None = None,
                         *, unconfigured_host: bool = False, **options) -> "Coordinator":
        """Start a fresh process from host-supplied current settings, never old leases."""
        coordinator = cls(**options)
        audio_clear = None
        if cue_id is not None:
            audio_clear = coordinator._send("audio", {"operation": "clear", "baseline": None, "text": None})
        dispatched = coordinator.set_baseline(generation, cue_id, text, suppress_audio_restore=True)
        # A synthetic safe-default generation is not a configured generation.
        # Permit one first real host reload at the same generation, then close it.
        coordinator._allow_initial_host_generation = unconfigured_host
        coordinator.recovery_result = {"previous_output": "unknown",
                                       "audio_clear": audio_clear,
                                       "baseline_dispatch": dispatched}
        return coordinator

    def record_feedback(self, source_id: str, correlation_id: str, ttl_s: float = 60) -> None:
        """Register one expected echo from a specific source; no wildcard matching."""
        identifier = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}"
        if (not isinstance(source_id, str) or not re.fullmatch(identifier, source_id) or
                not isinstance(correlation_id, str) or not re.fullmatch(identifier, correlation_id) or
                type(ttl_s) not in (int, float) or not 0 < ttl_s <= 300):
            raise ValueError("invalid feedback registration")
        with self._lock:
            now = self.monotonic_clock()
            self._prune(now)
            if len(self._feedback) >= 1024 and (source_id, correlation_id) not in self._feedback:
                self._feedback.pop(next(iter(self._feedback)))
            self._feedback[(source_id, correlation_id)] = now + ttl_s

    def set_baseline(self, generation: int, cue_id: str | None, text: str | None = None,
                     suppress_audio_restore: bool = False) -> dict:
        """Host-owned baseline update; an active lease continues until it ends."""
        with self._lock:
            self._prune(self.monotonic_clock())
            if type(generation) is not int or generation <= self._baseline_generation:
                raise ValueError("baseline generation must increase")
            if cue_id is not None and (not isinstance(cue_id, str) or
                                       not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", cue_id)):
                raise ValueError("invalid baseline cue")
            if not _valid_baseline_text(text):
                raise ValueError("invalid baseline text")
            self._baseline_generation = generation
            self._allow_initial_host_generation = False
            self._baseline_plan = None if cue_id is None else {
                "operation": "restore", "cue_id": cue_id, "status": "baseline",
                "severity": "info", "source_id": "local.baseline", "confidence": "known",
                "subject_id": None, "text": text, "expires_at": None,
                "baseline": cue_id, "baseline_generation": generation}
            return self._reconcile(suppress_audio_restore=suppress_audio_restore or self._audio_resuming())

    def set_policy(self, policy: QuietPolicy) -> dict:
        with self._lock:
            self._prune(self.monotonic_clock())
            self.policy = policy
            state = self._reconcile(suppress_audio_restore=self._audio_resuming())
            changed = self._sync_policy()
            return state if state["result"] != "unchanged" else changed

    def set_host_configuration(self, generation: int, cue_id: str | None,
                               text: str | None, policy: QuietPolicy) -> dict:
        """Apply a validated host baseline and quiet policy under one lock."""
        if not isinstance(policy, QuietPolicy):
            raise ValueError("invalid quiet policy")
        with self._lock:
            first_host_generation = (self._allow_initial_host_generation and
                                     generation == self._baseline_generation)
            if (type(generation) is not int or
                    (generation <= self._baseline_generation and not first_host_generation)):
                raise ValueError("baseline generation must increase")
            if cue_id is not None and (not isinstance(cue_id, str) or
                                       not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", cue_id)):
                raise ValueError("invalid baseline cue")
            if not _valid_baseline_text(text):
                raise ValueError("invalid baseline text")
            if cue_id is None and text is not None:
                raise ValueError("baseline text requires a cue")
            self._prune(self.monotonic_clock())
            self.policy = policy
            self._baseline_generation = generation
            self._allow_initial_host_generation = False
            if first_host_generation:
                self._emitted_baseline_generation = -1
            self._baseline_plan = None if cue_id is None else {
                "operation": "restore", "cue_id": cue_id, "status": "baseline",
                "severity": "info", "source_id": "local.baseline", "confidence": "known",
                "subject_id": None, "text": text, "expires_at": None,
                "baseline": cue_id, "baseline_generation": generation}
            state = self._reconcile(suppress_audio_restore=self._audio_resuming())
            changed = self._sync_policy()
            return state if state["result"] != "unchanged" else changed

    def _audio_resuming(self) -> bool:
        return not self._last_permitted["audio"] and self.policy.permits("audio", self.clock())

    def _current_plan(self) -> dict:
        top = self._top()
        if top is not None:
            return {**self._leases[top].plan, "operation": "restore"}
        return self._baseline_plan or {"operation": "clear", "baseline": None, "text": None}

    def _sync_policy(self) -> dict:
        """Clear newly muted outputs; restore visible state when allowed again."""
        results = {}
        now = self.clock()
        for channel in CHANNELS:
            permitted = self.policy.permits(channel, now)
            before = self._last_permitted[channel]
            self._last_permitted[channel] = permitted
            if before and not permitted:
                clear = {"operation": "clear", "baseline": None, "text": None}
                needs_clear = self._applied.get(channel) is not None or channel in self._pending
                self._desired[channel] = clear
                if needs_clear:
                    results[channel] = self._send(channel, clear)
            elif not before and permitted:
                if channel == "audio":
                    # Never replay an old cue/sound on resume. A failed clear stays pending.
                    self._desired[channel] = {"operation": "clear", "baseline": None, "text": None}
                    continue
                desired = self._current_plan()
                if channel == "text" and desired["operation"] != "clear" and desired["text"] is None:
                    desired = {"operation": "clear", "baseline": None, "text": None}
                self._desired[channel] = desired
                if desired["operation"] != "clear":
                    results[channel] = self._send(channel, desired)
        return {"result": "reconciled" if results else "unchanged", "channels": results}

    def _prune(self, now: float) -> None:
        for key, end in list(self._retired_subjects.items()):
            if end <= now:
                del self._retired_subjects[key]
        for key, end in list(self._feedback.items()):
            if end <= now:
                del self._feedback[key]
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
        for source, points in list(self._terminal_rate.items()):
            recent = [point for point in points if point > now - 60]
            if recent:
                self._terminal_rate[source] = recent
            else:
                del self._terminal_rate[source]
        for key, lease in list(self._leases.items()):
            if lease.expiry <= now:
                del self._leases[key]
                self._retire(key, lease, now)

    def _retire(self, key: tuple[str, str], lease: _Lease, now: float) -> None:
        # Only an explicit subject identifies a renewable run. A subjectless event
        # has its own bounded replay record, not a long-lived generation tombstone.
        if lease.plan["subject_id"] is None:
            return
        # Any end, including early expiry/cancel, blocks silent resurrection;
        # a distinct subject explicitly starts a new run.
        until = lease.first_seen + 2 * self.max_lease_age_s
        if until > now:
            # Admission reserved this slot while the subject was active.
            # Never evict an unexpired tombstone to make room for churn.
            self._retired_subjects[key] = until

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
        if outcome == "unsupported":
            return "unsupported"
        # An ambiguous receipt must not be retried automatically.
        return "unknown"

    def _dispatch(self, plan: dict, suppress_audio_restore: bool = False,
                  suppress_audio_cue: bool = False) -> dict[str, str]:
        results = {}
        for channel in CHANNELS:
            if channel == "audio" and plan["operation"] == "cue" and suppress_audio_cue:
                # A refreshed decision card changes its visible state, but its
                # first sound is never replayed (including after an ambiguous
                # initial receipt). Leave any current one-shot playback alone.
                results[channel] = "suppressed"
                continue
            if channel == "audio" and plan["operation"] == "restore" and suppress_audio_restore:
                self._desired[channel] = {"operation": "clear", "baseline": None, "text": None}
                results[channel] = "suppressed"
                continue
            if plan["operation"] != "clear" and not self.policy.permits(channel, self.clock()):
                # Keep a pending clear across quiet-state changes and new cue plans.
                self._desired[channel] = {"operation": "clear", "baseline": None, "text": None}
                results[channel] = "suppressed"
                continue
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

    def _reconcile(self, suppress_audio_restore: bool = False) -> dict:
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
        return {"result": "reconciled", "plan": plan,
                "channels": self._dispatch(plan, suppress_audio_restore)}

    def tick(self) -> dict:
        """Expire leases and dispatch a clear or restored baseline, without new input."""
        with self._lock:
            self._prune(self.monotonic_clock())
            state = self._reconcile(suppress_audio_restore=self._audio_resuming())
            policy = self._sync_policy()
            return state if state["result"] != "unchanged" else policy

    def text_snapshot(self, limit: int = 32) -> dict:
        """Bounded read-only projection of active leases for a local text view.

        The coordinator remains the sole owner of cue lifetimes and priority.
        Call tick separately to dispatch expiry transitions to output sinks.
        """
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("text snapshot limit must be within 1..32")
        with self._lock:
            if not self.policy.permits("text", self.clock()):
                return {"quiet": True, "total": 0, "source_totals": {},
                        "active_revisions": [], "entries": []}
            now = self.monotonic_clock()
            live = [(key, lease) for key, lease in self._leases.items() if lease.expiry > now]
            live.sort(key=lambda item: (item[1].priority, item[1].sequence), reverse=True)
            source_totals: dict[str, int] = {}
            active_revisions = []
            for key, lease in live:
                source = lease.plan["source_id"]
                source_totals[source] = source_totals.get(source, 0) + 1
                active_revisions.append((f"{key[0]}/{key[1]}", lease.sequence))
            entries = []
            for key, lease in live[:limit]:
                plan = lease.plan
                entries.append({"entry_id": f"{key[0]}/{key[1]}", "revision": lease.sequence,
                                "source_id": plan["source_id"],
                                "subject_id": plan["subject_id"], "cue_id": plan["cue_id"],
                                "status": plan["status"], "severity": plan["severity"],
                                "confidence": plan["confidence"], "text": plan["text"],
                                "occurred_at": plan["occurred_at"], "expires_at": plan["expires_at"],
                                "group": plan["group"], "count": plan["count"]})
            return {"quiet": False, "total": len(live), "source_totals": source_totals,
                    "active_revisions": active_revisions, "entries": entries}

    def _remember(self, key: tuple[str, str], event_key: tuple[str, str],
                  expiry: float, event: CueEvent) -> None:
        if len(self._seen) >= MAX_REPLAY_RECORDS:
            # A state-clearing cancellation may bypass admission, but cannot
            # discard another event's live replay protection to do so.
            return
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
        monotonic_now = self.monotonic_clock()
        self._prune(monotonic_now)
        self._reconcile(suppress_audio_restore=self._audio_resuming())
        self._sync_policy()
        if (event.source_id, event.correlation_id) in self._feedback:
            return {"result": "suppressed", "reason": "feedback_loop", "channels": {}}
        expiry = event.occurred_at.timestamp() + event.ttl_ms / 1000
        if event.occurred_at.timestamp() > now + 60 or event.observed_at.timestamp() > now + 60:
            return {"result": "suppressed", "reason": "future", "channels": {}}
        if expiry <= now:
            return {"result": "suppressed", "reason": "stale", "channels": {}}
        deadline = monotonic_now + (expiry - now)
        key = (event.source_id, event.idempotency_key)
        event_key = (event.source_id, event.event_id)
        prior = self._seen.get(key)
        if prior is None and event_key in self._seen_events:
            prior = self._seen.get(self._seen_events[event_key])
        if prior is not None:
            if prior[1] == event:
                return {"result": "suppressed", "reason": "duplicate", "channels": {}}
            return {"result": "rejected", "reason": "replay_conflict", "channels": {}}
        # Keep one-shot event IDs and renewable subject IDs in separate namespaces.
        lease_key = (event.source_id, f"subject:{event.subject_id}" if event.subject_id is not None
                     else f"event:{event.event_id}")
        cancellation = event.status == "cancelled"
        if cancellation and lease_key not in self._leases:
            return {"result": "suppressed", "reason": "unknown_subject", "channels": {}}
        if not cancellation:
            if lease_key in self._retired_subjects:
                return {"result": "suppressed", "reason": "lease_limit", "channels": {}}
            existing = self._leases.get(lease_key)
            if existing is not None and existing.plan["status"] in TERMINAL_STATUSES:
                return {"result": "suppressed", "reason": ("duplicate" if event.status in TERMINAL_STATUSES
                                                          else "lease_limit"), "channels": {}}
            terminal_reserve = (event.status in TERMINAL_STATUSES and
                                event.subject_id is not None and existing is not None)
            replay_limit = (MAX_REPLAY_RECORDS if terminal_reserve else
                            MAX_REPLAY_RECORDS - TERMINAL_REPLAY_RESERVE)
            if (len(self._seen) >= replay_limit or
                    (len(self._leases) >= 4096 and existing is None)):
                return {"result": "suppressed", "reason": "capacity", "channels": {}}
            if (event.subject_id is not None and existing is None and
                    len(self._retired_subjects) + sum(
                        lease.plan["subject_id"] is not None for lease in self._leases.values()
                    ) >= MAX_SUBJECT_GENERATIONS):
                return {"result": "suppressed", "reason": "capacity", "channels": {}}
            first_seen = existing.first_seen if existing else monotonic_now
            renewals = existing.renewals + 1 if existing else 0
            deadline = min(deadline, first_seen + self.max_lease_age_s)
            if deadline <= monotonic_now:
                return {"result": "suppressed", "reason": "lease_limit", "channels": {}}
            if terminal_reserve:
                recent = self._terminal_rate.get(event.source_id, [])
                if len(recent) >= TERMINAL_RATE_PER_SOURCE:
                    return {"result": "suppressed", "reason": "rate_limit", "channels": {}}
                if len(self._terminal_rate) >= 4096 and event.source_id not in self._terminal_rate:
                    return {"result": "suppressed", "reason": "source_limit", "channels": {}}
                self._terminal_rate[event.source_id] = recent + [monotonic_now]
            else:
                if len(self._rate) >= 256 and event.source_id not in self._rate:
                    return {"result": "suppressed", "reason": "source_limit", "channels": {}}
                recent = self._rate.get(event.source_id, [])
                if self.cooldown_seconds and recent and monotonic_now - recent[-1] < self.cooldown_seconds:
                    return {"result": "suppressed", "reason": "cooldown", "channels": {}}
                if len(recent) >= 10:
                    return {"result": "suppressed", "reason": "rate_limit", "channels": {}}
                self._rate[event.source_id] = recent + [monotonic_now]
            expiry = now + (deadline - monotonic_now)
        else:
            first_seen = monotonic_now
            renewals = 0
        self._remember(key, event_key, deadline, event)
        if cancellation:
            ended = self._leases.pop(lease_key, None)
            if ended is not None:
                self._retire(lease_key, ended, monotonic_now)
            reconciliation = self._reconcile()
            return {"result": "accepted", "reason": None, "plan": reconciliation.get("plan"),
                    "channels": reconciliation["channels"]}
        existing = self._leases.get(lease_key)
        decision_refresh = (existing is not None and event.subject_id is not None and
                            existing.plan["status"] == event.status == "needs_attention")
        self._sequence += 1
        plan = {"operation": "cue", "cue_id": event.cue_id, "status": event.status,
                "severity": event.severity, "source_id": event.source_id,
                "confidence": event.confidence, "subject_id": event.subject_id,
                "text": event.text, "occurred_at": event.occurred_at.isoformat(),
                "group": event.metadata.get("group"), "count": event.metadata.get("count"),
                "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
                "baseline": event.cue_id}
        self._leases[lease_key] = _Lease(deadline, first_seen, renewals, {"info": 1, "warning": 2,
                                                 "critical": 3}[event.severity], self._sequence, plan)
        if self._top() != lease_key:
            return {"result": "accepted", "reason": None, "plan": plan,
                    "channels": {channel: "preempted" for channel in CHANNELS}}
        self._active_key = lease_key
        return {"result": "accepted", "reason": None, "plan": plan,
                "channels": self._dispatch(plan, suppress_audio_cue=decision_refresh)}
