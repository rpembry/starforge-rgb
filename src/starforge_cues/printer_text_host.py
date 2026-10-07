"""Foreground printer text session; importing does not connect or show UI."""

from datetime import datetime, timezone
from pathlib import Path
import socket
from threading import RLock, Thread

from .printer_ingress import (BoundPrinterPublisher, PrinterIngressSettings,
                              registered_printer_host)
from .printer_mqtt import P1ReportNormalizer, collect_once, diagnostic_code
from .text_stack import TextProjectionSink


class PrinterTextSink(TextProjectionSink):
    """Hold one proven terminal card in this foreground window until dismissal."""

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._card: dict | None = None

    def dispatch(self, channel: str, plan: dict) -> str:
        result = super().dispatch(channel, plan)
        expected = {"printer.completed": ("succeeded", "Print finished."),
                    "printer.failed": ("failed", "Print failed.")}
        if (plan.get("operation") == "cue" and plan.get("cue_id") in expected and
                (plan.get("status"), plan.get("text")) == expected[plan["cue_id"]]):
            occurred = datetime.fromisoformat(plan["occurred_at"]).astimezone(timezone.utc)
            time_label = occurred.strftime("%H:%M UTC")
            source = plan["source_id"]
            body = plan["text"]
            with self._lock:
                self._card = {"row_id": "printer:held", "source_id": source,
                              "severity": plan["severity"], "status": plan["status"],
                              "confidence": plan["confidence"], "time": time_label,
                              "text": body, "group": None, "count": 1,
                              "label": (f"{plan['severity'].title()} {plan['status']}, "
                                        f"source {source}, known provenance, {time_label}, {body}")}
        return result

    def pinned_row(self) -> dict | None:
        with self._lock:
            return dict(self._card) if self._card is not None else None

    def dismiss(self) -> None:
        with self._lock:
            self._card = None


def with_printer_card(view: dict, sink: PrinterTextSink,
                      excluded_sources: frozenset[str]) -> dict:
    """A presentation hold; hidden by quiet, lock or source exclusion."""
    if view["hidden"]:
        return view
    card = sink.pinned_row()
    if (card is None or card["source_id"] in excluded_sources or
            any(row["source_id"] == card["source_id"] for row in view["rows"])):
        return view
    return {**view, "rows": [card] + view["rows"][:7]}


class PrinterForegroundSession:
    """One owned collector, listener and coordinator; no persistent service."""

    def __init__(self, settings: PrinterIngressSettings, normalizer: P1ReportNormalizer,
                 coordinator, client):
        self.normalizer = normalizer
        self.client = client
        self.host = registered_printer_host(settings, normalizer, coordinator)
        self.publisher = BoundPrinterPublisher(Path(self.host.listener.server_address))
        self.server_thread: Thread | None = None
        self.collector_thread: Thread | None = None
        self.summary: dict | None = None
        self.error: str | None = None
        self.receipts: list[dict] = []
        self._close_result: dict | None = None

    def _deliver(self, event) -> None:
        receipt = self.publisher.publish(event)
        self.receipts.append(receipt)
        if receipt["result"] != "accepted":
            raise RuntimeError("printer ingress did not accept terminal event")

    def _collect(self) -> None:
        try:
            collect_once(self.client, self.normalizer, self._deliver,
                         on_summary=lambda value: setattr(self, "summary", value))
        except Exception as exc:
            self.error = diagnostic_code(exc)

    def start(self) -> None:
        if self.server_thread is not None:
            raise RuntimeError("printer foreground session already started")
        self.server_thread = Thread(target=self.host.listener.serve_forever, daemon=True)
        self.server_thread.start()
        self.collector_thread = Thread(target=self._collect, daemon=True)
        self.collector_thread.start()

    def close(self) -> dict:
        if self._close_result is not None:
            return dict(self._close_result)
        stream = getattr(self.client, "stream", None)
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass
        if self.server_thread is not None:
            self.host.listener.shutdown()
            self.server_thread.join(timeout=3)
        self.host.listener.close()
        if self.collector_thread is not None:
            self.collector_thread.join(timeout=3)
        stopped = self.collector_thread is None or not self.collector_thread.is_alive()
        if not stopped:
            self.error = "collector_stop_unconfirmed"
        self._close_result = {"listener_closed": True, "collector_stopped": stopped}
        return dict(self._close_result)


def text_observation_ready(config_status: str, text_snapshot: dict,
                           *, unlocked: bool, banners_enabled: bool) -> bool:
    """Gate any printer network read on an actually displayable text channel."""
    return (config_status == "loaded" and text_snapshot.get("quiet") is False and
            unlocked is True and banners_enabled is True)
