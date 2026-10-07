"""Opt-in GTK4 regular window for the coordinator's stacked text projection.

Importing this module does not contact the desktop, start a socket, or show UI.
Only an explicit foreground command does so. No GNOME extension is installed.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from .contract import CueEvent
from .core import Coordinator
from .text_stack import TextProjectionSink, TextStackModel


def _synthetic_coordinator(sink: TextProjectionSink) -> Coordinator:
    coordinator = Coordinator({"text": sink})
    now = datetime.now(timezone.utc).isoformat()
    for index, (source, confidence, body) in enumerate((
            ("synthetic.build", "known", "Synthetic build completed."),
            ("synthetic.audio", "inferred", None),
            ("synthetic.build", "unknown", "Synthetic attention needed."))):
        identifier = f"preview-{index}"
        coordinator.handle(CueEvent.from_mapping({
            "version": 1, "event_id": identifier, "cue_id": "job.preview",
            "source_id": source, "confidence": confidence, "subject_id": identifier,
            "origin_id": "synthetic.preview", "occurred_at": now, "observed_at": now,
            "status": "unknown" if confidence == "unknown" else "succeeded",
            "severity": "warning" if confidence == "unknown" else "info",
            "ttl_ms": 300000, "idempotency_key": identifier, "text": body,
            "metadata": {"group": "preview"} if index != 1 else {}}))
    return coordinator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foreground stacked text window")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic-preview", action="store_true",
                      help="show only built-in synthetic cues; no socket or configuration")
    mode.add_argument("--serve", action="store_true",
                      help="run a foreground restricted local cue receiver")
    mode.add_argument("--printer-text", action="store_true",
                      help="one foreground P1 completion observation with text only")
    parser.add_argument("--socket", type=Path, help="required for --serve")
    parser.add_argument("--config", type=Path, help="required protected settings file for --serve")
    parser.add_argument("--ingress-config", type=Path, help="private local printer account settings")
    parser.add_argument("--printer-host", help="private printer IPv4 address")
    parser.add_argument("--printer-serial", help="printer serial from its settings screen")
    parser.add_argument("--printer-ca-file", type=Path, help="verified CA PEM")
    parser.add_argument("--printer-peer-sha256", help="confirmed exact leaf fingerprint")
    parser.add_argument("--printer-credential-file", type=Path,
                        help="existing owned private access-code file")
    parser.add_argument("--bambu-legacy-ca", action="store_true",
                        help="explicit CA-profile compatibility; keep CA validation")
    parser.add_argument("--manual-audio-sink", help="exact PipeWire sink name; enables only manual.test audio")
    parser.add_argument("--manual-audio-gain", type=float, default=0.05,
                        help="per-stream gain, normally at most 0.05")
    parser.add_argument("--commissioning-override", action="store_true",
                        help="explicit one-off gain gate up to 0.15; does not change system volume")
    parser.add_argument("--exclude-source", action="append", default=[],
                        help="hide this source from indicators and history")
    args = parser.parse_args(argv)
    if args.serve and (args.socket is None or args.config is None):
        parser.error("--serve requires --socket and --config")
    printer_values = (args.ingress_config, args.printer_host, args.printer_serial,
                      args.printer_ca_file, args.printer_peer_sha256,
                      args.printer_credential_file)
    if args.printer_text:
        if (args.config is None or any(value is None for value in printer_values[:5]) or
                args.socket is not None or args.manual_audio_sink is not None or
                args.manual_audio_gain != 0.05 or args.commissioning_override):
            parser.error("--printer-text requires protected settings and printer identity; no audio")
    elif any(value is not None for value in printer_values) or args.bambu_legacy_ca:
        parser.error("printer options require --printer-text")
    if args.synthetic_preview and (args.socket is not None or args.config is not None):
        parser.error("synthetic preview does not use a socket or configuration")
    if args.synthetic_preview and (args.manual_audio_sink is not None or
                                   args.manual_audio_gain != 0.05 or args.commissioning_override):
        parser.error("synthetic preview does not use audio output")
    if len(args.exclude_source) > 32:
        parser.error("at most 32 source exclusions")

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        parser.error(f"GTK4 is unavailable: {exc}")

    if args.serve:
        from .manual_host import manual_sinks
        try:
            sinks = manual_sinks(args.manual_audio_sink, gain=args.manual_audio_gain,
                                 commissioning_override=args.commissioning_override)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.printer_text:
        from .printer_text_host import PrinterTextSink
        sinks = {"text": PrinterTextSink()}
    else:
        sinks = {"text": TextProjectionSink()}
    sink = sinks["text"]
    if args.synthetic_preview:
        coordinator = _synthetic_coordinator(sink)
        config_status = "synthetic"
    elif args.printer_text:
        from .core import SourceCapabilities
        from .host_config import recover_private_config
        from .printer_ingress import read_private_printer_ingress_settings, printer_binding
        from .printer_mqtt import (P1ReportNormalizer, SubscribeOnlyP1Client,
                                   default_credential_path)
        from .printer_text_host import PrinterForegroundSession
        try:
            ingress = read_private_printer_ingress_settings(args.ingress_config)
            normalizer = P1ReportNormalizer(args.printer_serial)
            binding = printer_binding(normalizer)
            source = next(iter(binding.source_ids))
            capabilities = SourceCapabilities(binding.statuses, True, True)
            coordinator, config_status = recover_private_config(
                args.config, sinks=sinks, sources={source: capabilities})
            client = SubscribeOnlyP1Client(
                args.printer_host, args.printer_serial, args.printer_ca_file,
                args.printer_peer_sha256,
                args.printer_credential_file or default_credential_path(),
                bambu_legacy_ca=args.bambu_legacy_ca)
            printer_session = PrinterForegroundSession(ingress, normalizer,
                                                       coordinator, client)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"printer text setup unavailable: {type(exc).__name__}")
    else:
        from .host_config import recover_private_config
        coordinator, config_status = recover_private_config(args.config, sinks=sinks)
    if not args.printer_text:
        printer_session = None
    model = TextStackModel(excluded_sources=frozenset(args.exclude_source))

    class WindowApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=("org.starforge.CuesPrinterText" if args.printer_text
                                             else "org.starforge.CuesText"))
            self.window = None
            self.server = None
            self.server_thread = None
            self.rendered = None
            self.row_ids = {}
            self.closed = False
            self.cleanup = None
            self.printer_session = printer_session
            self.status_label = None

        def _unlocked(self) -> bool:
            # Failure is private by default. GetActive is read-only; no lock changes.
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                response = bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                         "org.gnome.ScreenSaver", "GetActive", None,
                                         GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE,
                                         1000, None)
                return response.unpack()[0] is False
            except Exception:
                return False

        def _label(self, content: str, *, wrap: bool = True) -> Gtk.Label:
            label = Gtk.Label(label=content, xalign=0)
            label.set_wrap(wrap)
            label.set_selectable(False)
            return label

        def _render(self) -> bool:
            coordinator.tick()
            if self.status_label is not None:
                worker = self.printer_session.collector_thread
                if worker is None:
                    status = "Printer observation starting"
                elif worker.is_alive():
                    state = self.printer_session.normalizer.last_known_state or "unobserved"
                    status = f"Printer observation active · last explicit state: {state}"
                else:
                    status = ("Printer observation ended" +
                              (f" · {self.printer_session.error}"
                               if self.printer_session.error else ""))
                self.status_label.set_label(status)
            unlocked = self._unlocked()
            if args.printer_text:
                try:
                    unlocked = unlocked and Gio.Settings.new(
                        "org.gnome.desktop.notifications").get_boolean("show-banners")
                except Exception:
                    unlocked = False
            view = model.refresh(coordinator.text_snapshot(), unlocked=unlocked,
                                 elapsed=coordinator.monotonic_clock())
            if args.printer_text:
                from .printer_text_host import with_printer_card
                view = with_printer_card(view, sinks["text"], model.excluded_sources)
                if view["hidden"] and self.status_label is not None:
                    self.status_label.set_label("Printer observation · details hidden by quiet or lock")
            else:
                from .manual_host import with_manual_card
                view = with_manual_card(view, sinks["text"], model.excluded_sources)
            signature = (view["hidden"], tuple((row["row_id"], row["label"])
                                               for row in view["rows"]),
                         tuple((item["entry_id"], item["revision"], item["recorded_elapsed"])
                               for item in view["history"]), view["overflow"],
                         view["truncated"])
            if signature == self.rendered:
                return True
            self.rendered = signature
            self.live.remove_all()
            self.history.remove_all()
            self.row_ids.clear()
            if view["hidden"]:
                self.live.append(self._label("Indicators and history hidden by quiet policy or screen lock."))
            elif not view["rows"]:
                self.live.append(self._label("No active cues."))
            for item in view["rows"]:
                row = Gtk.ListBoxRow()
                row.update_property([Gtk.AccessibleProperty.LABEL], [item["label"]])
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                summary = f"{item['severity'].title()} · {item['source_id']} · {item['time']}"
                if item["group"] is not None:
                    summary += f" · {item['group']}"
                if item["count"] > 1:
                    summary += f" · {item['count']} items"
                content.append(self._label(summary))
                content.append(self._label(f"{item['status'].replace('_', ' ')} · "
                                           f"{item['confidence']} provenance"))
                if item["text"] is not None:
                    content.append(self._label(item["text"]))
                dismiss = Gtk.Button(label="Dismiss indicator")
                dismiss.connect("clicked", lambda _button, row_id=item["row_id"],
                                source=item["source_id"]: self._dismiss_row(row_id, source))
                content.append(dismiss)
                row.set_child(content)
                self.live.append(row)
                self.row_ids[row] = (item["row_id"], item["source_id"])
            if view["overflow"]:
                self.live.append(self._label(f"{view['overflow']} more groups in the bounded snapshot."))
            if view["truncated"]:
                self.live.append(self._label(f"{view['truncated']} additional cues outside the bounded snapshot."))
            if not view["hidden"]:
                for item in view["history"]:
                    occurred = datetime.fromisoformat(item["occurred_at"]).astimezone(timezone.utc)
                    self.history.append(self._label(
                        f"{item['severity'].title()} · {item['source_id']} · "
                        f"{occurred:%H:%M} UTC · {item['status'].replace('_', ' ')} · "
                        f"{item['confidence']} provenance"))
            return True

        def _key(self, _controller, keyval, _keycode, _state):
            if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
                row = self.live.get_selected_row()
                if row in self.row_ids:
                    self._dismiss_row(*self.row_ids[row])
                    return True
            return False

        def _dismiss_row(self, row_id: str, source_id: str):
            model.dismiss(row_id)
            pinned = getattr(sinks["text"], "pinned_row", lambda: None)()
            if pinned is not None and pinned["source_id"] == source_id:
                sinks["text"].dismiss()
            self._render()

        def _close(self, *_args):
            if not self.closed:
                if self.printer_session is not None:
                    self.cleanup = self.printer_session.close()
                else:
                    from .manual_host import close_foreground
                    self.cleanup = close_foreground(self.server, sinks.get("audio"))
                self.server = None
                self.closed = True
            return False

        def do_activate(self):
            if self.window is not None:
                self.window.present()
                return
            self.window = Gtk.ApplicationWindow(application=self, title="Starforge cues")
            monitor_list = Gdk.Display.get_default().get_monitors()
            monitor = monitor_list.get_item(0) if monitor_list.get_n_items() else None
            if monitor is not None:
                geometry = monitor.get_geometry()
                self.window.set_default_size(min(420, int(geometry.width * 0.8)),
                                             min(600, int(geometry.height * 0.8)))
            else:
                self.window.set_default_size(420, 600)
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            for edge in ("top", "bottom", "start", "end"):
                getattr(outer, f"set_margin_{edge}")(12)
            outer.append(self._label("Active cues (arrow keys to navigate; Delete to dismiss)"))
            if args.printer_text:
                self.status_label = self._label("Printer observation starting")
                outer.append(self.status_label)
            self.live = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._key)
            self.live.add_controller(keys)
            live_scroll = Gtk.ScrolledWindow()
            live_scroll.set_vexpand(True)
            live_scroll.set_child(self.live)
            outer.append(live_scroll)
            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            controls.append(self._label("Recent metadata (message bodies are not retained)"))
            clear = Gtk.Button(label="Clear history")
            clear.connect("clicked", lambda _button: (model.clear_history(), self._render()))
            controls.append(clear)
            close = Gtk.Button(label="Close window")
            close.connect("clicked", lambda _button: self.window.close())
            controls.append(close)
            outer.append(controls)
            self.history = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            history_scroll = Gtk.ScrolledWindow()
            history_scroll.set_vexpand(True)
            history_scroll.set_child(self.history)
            outer.append(history_scroll)
            self.window.set_child(outer)
            self.window.connect("close-request", self._close)
            if args.serve:
                from .transport import LocalServer
                try:
                    if "audio" in sinks:
                        from .manual_host import manual_event_allowed
                        self.server = LocalServer(args.socket, coordinator,
                                                  event_filter=manual_event_allowed)
                    else:
                        self.server = LocalServer(args.socket, coordinator)
                except (OSError, RuntimeError) as exc:
                    parser.error(f"local receiver unavailable: {exc}")
                self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
                self.server_thread.start()
            self._render()
            GLib.timeout_add(500, self._render)
            self.window.present()
            if args.printer_text:
                try:
                    self.printer_session.start()
                except (OSError, RuntimeError):
                    self.window.close()
                    parser.error("printer text session unavailable")

    # Only a generic status is emitted; never print a settings path or cue body.
    print(f"text window mode: {config_status}; manual audio: "
          f"{'armed' if 'audio' in sinks else 'disabled'}")
    app = WindowApp()
    outcome = app.run([])
    if printer_session is not None:
        if printer_session.summary is not None:
            print("printer session: " + json.dumps(printer_session.summary, sort_keys=True))
        if printer_session.error is not None:
            print(f"printer session ended: {printer_session.error}")
        print(f"printer deliveries: {len(printer_session.receipts)}")
    if app.cleanup is not None:
        print(f"foreground cleanup: {app.cleanup}")
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
