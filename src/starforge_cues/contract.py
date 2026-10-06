"""Strict v1 producer contract. No device actions or policy knobs are accepted."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

MAX_BYTES = 8192
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
_RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
_FIELDS = frozenset({"version", "event_id", "cue_id", "source_id", "confidence", "subject_id",
                     "correlation_id", "origin_id", "occurred_at", "observed_at", "status",
                     "severity", "ttl_ms", "idempotency_key", "text", "metadata"})
_REQUIRED = _FIELDS - {"subject_id", "correlation_id", "text", "metadata"}
_STATUS = frozenset({"started", "progress", "succeeded", "failed", "needs_attention", "cancelled", "unknown"})
_SEVERITY = frozenset({"info", "warning", "critical"})


class ContractError(ValueError):
    pass


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError(f"{name}: invalid identifier")
    return value


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise ContractError(f"{name}: expected RFC3339 timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}: invalid timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise ContractError(f"{name}: UTC timestamp required")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class CueEvent:
    version: int
    event_id: str
    cue_id: str
    source_id: str
    confidence: str
    subject_id: str | None
    correlation_id: str | None
    origin_id: str
    occurred_at: datetime
    observed_at: datetime
    status: str
    severity: str
    ttl_ms: int
    idempotency_key: str
    text: str | None
    metadata: dict[str, str | int | bool]

    @classmethod
    def from_mapping(cls, value: object) -> "CueEvent":
        if not isinstance(value, dict):
            raise ContractError("event: expected object")
        if (missing := _REQUIRED - value.keys()):
            raise ContractError(f"missing fields: {', '.join(sorted(missing))}")
        if value.keys() - _FIELDS:
            raise ContractError("event: unknown field")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ContractError("version: unsupported")
        ids = {name: _identifier(value[name], name) for name in
               ("event_id", "cue_id", "source_id", "origin_id", "idempotency_key")}
        for name in ("subject_id", "correlation_id"):
            ids[name] = None if value.get(name) is None else _identifier(value[name], name)
        confidence = value["confidence"]
        if not isinstance(confidence, str) or confidence not in ("known", "inferred", "unknown"):
            raise ContractError("confidence: invalid")
        status = value["status"]
        if not isinstance(status, str) or status not in _STATUS:
            raise ContractError("status: invalid")
        severity = value["severity"]
        if not isinstance(severity, str) or severity not in _SEVERITY:
            raise ContractError("severity: invalid")
        ttl = value["ttl_ms"]
        if type(ttl) is not int or not 1 <= ttl <= 300_000:
            raise ContractError("ttl_ms: out of range")
        body = value.get("text")
        if body is not None and (not isinstance(body, str) or len(body) > 280 or any(ord(c) < 32 and c not in "\n\t" for c in body)):
            raise ContractError("text: invalid or too long")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict) or len(metadata) > 8:
            raise ContractError("metadata: expected at most 8 keys")
        for key, item in metadata.items():
            _identifier(key, "metadata key")
            if key not in {"label", "phase", "group", "count"}:
                raise ContractError("metadata: unsupported key")
            if key == "count":
                valid = type(item) is int and abs(item) <= 1_000_000
            else:
                valid = isinstance(item, str) and len(item) <= 80
            if not valid:
                raise ContractError("metadata: invalid value")
        occurred = _time(value["occurred_at"], "occurred_at")
        observed = _time(value["observed_at"], "observed_at")
        if observed < occurred:
            raise ContractError("observed_at: before occurred_at")
        if status == "cancelled" and ids["subject_id"] is None:
            raise ContractError("subject_id: required for cancellation")
        return cls(1, ids["event_id"], ids["cue_id"], ids["source_id"], confidence,
                   ids["subject_id"], ids["correlation_id"], ids["origin_id"], occurred,
                   observed, status, severity, ttl, ids["idempotency_key"], body, dict(metadata))

    @classmethod
    def from_json(cls, raw: bytes) -> "CueEvent":
        if len(raw) > MAX_BYTES:
            raise ContractError("event: too large")
        try:
            def unique_pairs(pairs):
                result = {}
                for key, val in pairs:
                    if key in result:
                        raise ContractError("event: duplicate JSON key")
                    result[key] = val
                return result
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError("event: invalid JSON") from exc
        return cls.from_mapping(value)
