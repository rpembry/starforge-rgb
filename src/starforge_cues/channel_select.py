"""Pure per-channel choices over one coordinator-owned lease snapshot."""

from dataclasses import dataclass
from typing import Mapping, Protocol


LeaseKey = tuple[str, str]


class LeaseView(Protocol):
    expiry: float
    priority: int
    sequence: int


@dataclass(frozen=True)
class ChannelSelection:
    """Internal lease keys only; no render payload or device commands."""

    text_keys: tuple[LeaseKey, ...]
    rgb_key: LeaseKey | None
    audio_key: LeaseKey | None


def _rank(item: tuple[LeaseKey, LeaseView]) -> tuple[int, int]:
    return item[1].priority, item[1].sequence


def ranked_live(leases: Mapping[LeaseKey, LeaseView], now: float | None = None
                ) -> tuple[tuple[LeaseKey, LeaseView], ...]:
    """Priority and recency order; the host limits active leases to 4096."""
    return tuple(sorted(((key, lease) for key, lease in leases.items()
                         if now is None or lease.expiry > now), key=_rank, reverse=True))


def rgb_winner(leases: Mapping[LeaseKey, LeaseView], now: float | None = None
               ) -> LeaseKey | None:
    """The single sustained winner, independent of text and audio selection."""
    live = ((key, lease) for key, lease in leases.items()
            if now is None or lease.expiry > now)
    winner = max(live, key=_rank, default=None)
    return None if winner is None else winner[0]


def select_channels(leases: Mapping[LeaseKey, LeaseView], now: float, *,
                    text_permitted: bool, unlocked: bool, rgb_permitted: bool,
                    audio_permitted: bool, admitted_key: LeaseKey | None = None,
                    admitted_status: str | None = None,
                    previous_status: str | None = None) -> ChannelSelection:
    """Select channel identities without mutating leases or dispatching output.

    Quiet/capability decisions arrive as host-owned per-channel booleans.
    `unlocked` defaults to no visibility by being a required argument.
    """
    ranked = ranked_live(leases, now)
    live_keys = tuple(key for key, _ in ranked)
    text_keys = live_keys if text_permitted and unlocked else ()
    rgb_key = live_keys[0] if rgb_permitted and live_keys else None
    audio_key = None
    if (audio_permitted and admitted_key in live_keys and
            admitted_status not in (None, "cancelled") and
            not (admitted_status == previous_status == "needs_attention")):
        audio_key = admitted_key
    return ChannelSelection(text_keys, rgb_key, audio_key)
