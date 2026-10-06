"""Portable lease lifetime bookkeeping; no policy dispatch or device I/O."""

from dataclasses import dataclass


MAX_SUBJECT_GENERATIONS = 2048


@dataclass(frozen=True)
class Lease:
    expiry: float
    first_seen: float
    renewals: int
    priority: int
    sequence: int
    plan: dict


class LeaseBook:
    """Own active leases and protected retired subject generations.

    The coordinator serializes access under its lock. A subject's protection
    slot transfers from active to retired at expiry or cancellation.
    """

    def __init__(self, max_lease_age_s: float):
        self.max_lease_age_s = max_lease_age_s
        self.leases: dict[tuple[str, str], Lease] = {}
        self.retired: dict[tuple[str, str], float] = {}

    def prune_retired(self, now: float) -> None:
        for key, end in list(self.retired.items()):
            if end <= now:
                del self.retired[key]

    def expire(self, now: float) -> None:
        for key, lease in list(self.leases.items()):
            if lease.expiry <= now:
                del self.leases[key]
                self.retire(key, lease, now)

    def retire(self, key: tuple[str, str], lease: Lease, now: float) -> None:
        # Subjectless events use their replay record, not a generation slot.
        if lease.plan["subject_id"] is None:
            return
        until = lease.first_seen + 2 * self.max_lease_age_s
        if until > now:
            # The slot was reserved on admission. Never evict protection.
            self.retired[key] = until

    def cancel(self, key: tuple[str, str], now: float) -> bool:
        ended = self.leases.pop(key, None)
        if ended is None:
            return False
        self.retire(key, ended, now)
        return True

    def put(self, key: tuple[str, str], lease: Lease) -> None:
        self.leases[key] = lease

    def has_subject_capacity(self, key: tuple[str, str]) -> bool:
        if key in self.leases:
            return True
        active_subjects = sum(lease.plan["subject_id"] is not None
                              for lease in self.leases.values())
        return len(self.retired) + active_subjects < MAX_SUBJECT_GENERATIONS

    def top(self) -> tuple[str, str] | None:
        if not self.leases:
            return None
        return max(self.leases, key=lambda key: (self.leases[key].priority,
                                                 self.leases[key].sequence))
