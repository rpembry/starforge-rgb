"""Portable, bounded view model for a regular application text window."""

from collections import OrderedDict
from datetime import datetime, timezone


def _visible_text(value: str | None) -> str | None:
    if value is None:
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
                type(history_age_s) is not int or not 60 <= history_age_s <= 86400 or
                not isinstance(excluded_sources, frozenset) or len(excluded_sources) > 32 or
                any(not isinstance(source, str) for source in excluded_sources)):
            raise ValueError("invalid text view bounds")
        self.max_visible = max_visible
        self.max_history = max_history
        self.history_age_s = history_age_s
        self.excluded_sources = excluded_sources
        self._dismissed: set[str] = set()
        self._history: list[dict] = []
        self._seen_history: set[str] = set()
        self._current_groups: dict[str, tuple[str, ...]] = {}

    def clear_history(self) -> None:
        self._history.clear()

    def dismiss(self, row_id: str) -> None:
        self._dismissed.update(self._current_groups.get(row_id, ()))

    def refresh(self, snapshot: dict, *, unlocked: bool, now: float) -> dict:
        """Render one coordinator snapshot; unknown lock state must pass unlocked=False."""
        self._history = [record for record in self._history
                         if record["recorded_at"] > now - self.history_age_s]
        if not unlocked or snapshot["quiet"]:
            self._current_groups = {}
            return {"hidden": True, "rows": [], "history": [], "overflow": 0}

        entries = [entry for entry in snapshot["entries"]
                   if entry["source_id"] not in self.excluded_sources]
        active_ids = {entry["entry_id"] for entry in entries}
        self._dismissed.intersection_update(active_ids)
        self._seen_history.intersection_update(active_ids)
        for entry in entries:
            identifier = entry["entry_id"]
            if identifier not in self._seen_history:
                self._seen_history.add(identifier)
                # Default history contains metadata only, never raw message text.
                self._history.append({"entry_id": identifier, "source_id": entry["source_id"],
                                      "status": entry["status"], "severity": entry["severity"],
                                      "confidence": entry["confidence"],
                                      "occurred_at": entry["occurred_at"], "recorded_at": now})
        self._history = self._history[-self.max_history:]

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for entry in entries:
            if entry["entry_id"] in self._dismissed:
                continue
            group = entry["group"]
            row_id = (f"group:{entry['source_id']}/{group}" if group is not None
                      else f"entry:{entry['entry_id']}")
            groups.setdefault(row_id, []).append(entry)
        self._current_groups = {row_id: tuple(item["entry_id"] for item in items)
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
        return {"hidden": False, "rows": rows,
                "history": list(reversed(self._history)),
                "overflow": max(0, len(groups) - self.max_visible)}
