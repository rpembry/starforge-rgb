"""Foreground printer text session; importing does not connect or show UI."""

from datetime import datetime, timezone
from pathlib import Path
import socket
from threading import Event, RLock, Thread

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
        self._stop_requested = Event()
        self._server_exited = Event()

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
        def serve():
            try:
                if not self._stop_requested.is_set():
                    self.host.listener.serve_forever(poll_interval=0.05)
            finally:
                self._server_exited.set()

        try:
            server = Thread(target=serve, daemon=True)
            server.start()
            self.server_thread = server  # A failed Thread.start is never joined.
            if not self.host.listener.service_ready.wait(timeout=2):
                raise RuntimeError("printer listener did not become ready")
            collector = Thread(target=self._collect, daemon=True)
            collector.start()
            self.collector_thread = collector
        except Exception as exc:
            self.close()
            raise RuntimeError("printer foreground startup failed") from exc

    def close(self) -> dict:
        if self._close_result is not None:
            return dict(self._close_result)
        self._stop_requested.set()
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
        if self.server_thread is not None and self.host.listener.service_ready.is_set():
            self.host.listener.shutdown()
        self.host.listener.close()
        if self.server_thread is not None:
            self.server_thread.join(timeout=3)
        if self.collector_thread is not None:
            self.collector_thread.join(timeout=3)
        server_stopped = self.server_thread is None or not self.server_thread.is_alive()
        stopped = self.collector_thread is None or not self.collector_thread.is_alive()
        if not server_stopped or not stopped:
            self.error = "foreground_stop_unconfirmed"
        self._close_result = {"listener_closed": True, "server_stopped": server_stopped,
                              "collector_stopped": stopped}
        return dict(self._close_result)


def text_observation_ready(config_status: str, text_snapshot: dict,
                           *, unlocked: bool, banners_enabled: bool) -> bool:
    """Gate any printer network read on an actually displayable text channel."""
    return (config_status == "loaded" and text_snapshot.get("quiet") is False and
            unlocked is True and banners_enabled is True)
