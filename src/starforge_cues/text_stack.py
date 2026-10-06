"""Portable, bounded view model for a regular application text window."""

from collections import OrderedDict
from datetime import datetime, timezone


def _visible_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(char if char.isprintable() or char in "\n\t" else "\ufffd" for char in value)


class TextProjectionSink:
    """One-plan receipt for the local view; acceptance never implies perception."""

    capabilities = frozenset({"text"})

    def __init__(self):
        self.last_plan: dict | None = None

    def dispatch(self, channel: str, plan: dict) -> str:
        if channel != "text":
            raise ValueError("unsupported channel")
        self.last_plan = dict(plan)
        return "accepted"


class TextStackModel:
    """Presentation state only; the coordinator owns leases and policy."""

    def __init__(self, *, max_visible: int = 8, max_history: int = 50,
                 history_age_s: int = 3600, excluded_sources: frozenset[str] = frozenset()):
        if (type(max_visible) is not int or not 1 <= max_visible <= 16 or
                type(max_history) is not int or not 1 <= max_history <= 100 or
                type(history_age_s) is not int or not 60 <= history_age_s <= 86400):
            raise ValueError("invalid text view bounds")
        self.max_visible = max_visible
        self.max_history = max_history
        self.history_age_s = history_age_s
        self._excluded_sources = self._checked_sources(excluded_sources)
        self._dismissed: set[tuple[str, int]] = set()
        self._history: list[dict] = []
        self._seen_history: dict[tuple[str, int], str] = {}
        self._current_groups: dict[str, tuple[tuple[str, int], ...]] = {}

    @staticmethod
    def _checked_sources(sources: frozenset[str]) -> frozenset[str]:
        if (not isinstance(sources, frozenset) or len(sources) > 32 or
                any(not isinstance(source, str) for source in sources)):
            raise ValueError("invalid source exclusions")
        return sources

    @property
    def excluded_sources(self) -> frozenset[str]:
        return self._excluded_sources

    def set_excluded_sources(self, sources: frozenset[str]) -> None:
        """Replace exclusions and purge matching in-memory history immediately."""
        checked = self._checked_sources(sources)
        self._excluded_sources = checked
        self._history = [record for record in self._history
                         if record["source_id"] not in checked]
        self._seen_history = {key: source for key, source in self._seen_history.items()
                              if source not in checked}

    def clear_history(self) -> None:
        self._history.clear()

    def dismiss(self, row_id: str) -> None:
        self._dismissed.update(self._current_groups.get(row_id, ()))

    def refresh(self, snapshot: dict, *, unlocked: bool, elapsed: float) -> dict:
        """Render a snapshot using injected monotonic elapsed time for retention."""
        self._history = [record for record in self._history
                         if record["recorded_elapsed"] > elapsed - self.history_age_s]
        if not unlocked or snapshot["quiet"]:
            self._current_groups = {}
            return {"hidden": True, "rows": [], "history": [],
                    "overflow": 0, "truncated": 0}

        entries = [entry for entry in snapshot["entries"]
                   if entry["source_id"] not in self.excluded_sources]
        # The detailed projection is capped at 32, while this identity index
        # covers every active lease (bounded by the coordinator's 4096 cap).
        # Missing detail is not authoritative evidence of cancellation/expiry.
        active_revisions = set(map(tuple, snapshot["active_revisions"]))
        self._dismissed.intersection_update(active_revisions)
        self._seen_history = {key: source for key, source in self._seen_history.items()
                              if key in active_revisions}
        for entry in entries:
            revision = (entry["entry_id"], entry["revision"])
            if revision not in self._seen_history:
                self._seen_history[revision] = entry["source_id"]
                # Default history contains metadata only, never raw message text.
                self._history.append({"entry_id": entry["entry_id"],
                                      "revision": entry["revision"],
                                      "source_id": entry["source_id"],
                                      "status": entry["status"], "severity": entry["severity"],
                                      "confidence": entry["confidence"],
                                      "occurred_at": entry["occurred_at"],
                                      "recorded_elapsed": elapsed})
        self._history = self._history[-self.max_history:]

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for entry in entries:
            if (entry["entry_id"], entry["revision"]) in self._dismissed:
                continue
            group = entry["group"] if isinstance(entry["group"], str) else None
            row_id = (f"group:{entry['source_id']}/{group}" if group is not None
                      else f"entry:{entry['entry_id']}")
            groups.setdefault(row_id, []).append(entry)
        self._current_groups = {row_id: tuple((item["entry_id"], item["revision"])
                                              for item in items)
                                for row_id, items in groups.items()}
        rows = []
        for row_id, items in list(groups.items())[:self.max_visible]:
            current = items[0]  # coordinator snapshot is priority/recency ordered
            body = _visible_text(current["text"])
            group_label = _visible_text(current["group"])
            declared = current["count"]
            count = max(len(items), declared if type(declared) is int and declared > 0 else 1)
            occurred = datetime.fromisoformat(current["occurred_at"]).astimezone(timezone.utc)
            time_label = occurred.strftime("%H:%M UTC")
            label = (f"{current['severity'].replace('_', ' ').title()} "
                     f"{current['status'].replace('_', ' ')}, source {current['source_id']}, "
                     f"{current['confidence']} provenance, {time_label}")
            if count > 1:
                label += f", {count} items"
            if group_label is not None:
                label += f", group {group_label}"
            if body is not None:
                label += f", {body}"
            rows.append({"row_id": row_id, "source_id": current["source_id"],
                         "severity": current["severity"], "status": current["status"],
                         "confidence": current["confidence"], "time": time_label,
                         "text": body, "group": group_label, "count": count, "label": label})
        eligible_total = sum(count for source, count in snapshot["source_totals"].items()
                             if source not in self.excluded_sources)
        return {"hidden": False, "rows": rows,
                "history": list(reversed(self._history)),
                "overflow": max(0, len(groups) - self.max_visible),
                "truncated": max(0, eligible_total - len(entries))}
