"""Manual text/audio path with fake output; no GTK or PipeWire calls."""

from datetime import datetime, timezone
import errno
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from starforge_cues.audio import FakeAudioBackend
from starforge_cues.core import Coordinator, QuietPolicy
from starforge_cues.manual_host import MANUAL_CUE, manual_event, manual_sinks
from starforge_cues.transport import LocalServer, submit

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class ManualHostTests(unittest.TestCase):
    def test_manual_event_drives_text_and_only_selected_fake_audio(self):
        backend = FakeAudioBackend(frozenset({"selected.speaker", "other.speaker"}),
                                   monotonic_clock=lambda: NOW.timestamp())
        sinks = manual_sinks("selected.speaker", backend=backend)
        core = Coordinator(sinks, clock=lambda: NOW.timestamp())
        event = manual_event(instant=NOW, event_id="synthetic-one")
        self.assertEqual(event.cue_id, MANUAL_CUE)
        result = core.handle(event)
        self.assertEqual(result["channels"]["text"], "accepted")
        self.assertEqual(result["channels"]["audio"], "accepted")
        self.assertEqual(sinks["text"].last_plan["text"], "Manual notification test.")
        self.assertEqual([(sink, gain) for sink, _, gain in backend.starts],
                         [("selected.speaker", 0.05)])
        self.assertEqual(core.handle(event)["reason"], "duplicate")
        self.assertEqual(len(backend.starts), 1)

    def test_quiet_and_missing_sink_prevent_audio_without_losing_text(self):
        for backend_sinks, policy in ((frozenset(), QuietPolicy()),
                                      (frozenset({"selected.speaker"}), QuietPolicy(quiet=True))):
            with self.subTest(backend_sinks=backend_sinks, policy=policy):
                backend = FakeAudioBackend(backend_sinks)
                sinks = manual_sinks("selected.speaker", backend=backend)
                core = Coordinator(sinks, clock=lambda: NOW.timestamp(), policy=policy)
                result = core.handle(manual_event(instant=NOW, event_id="synthetic-two"))
                self.assertEqual(result["result"], "accepted")
                self.assertEqual(backend.starts, [])
                self.assertIn(result["channels"]["audio"], {"suppressed", "unsupported"})

    def test_audio_opt_in_and_gain_bounds(self):
        self.assertEqual(set(manual_sinks()), {"text"})
        with self.assertRaises(ValueError):
            manual_sinks(gain=0.15)
        with self.assertRaises(ValueError):
            manual_sinks("selected.speaker", gain=0.15,
                         backend=FakeAudioBackend(frozenset({"selected.speaker"})))
        enabled = manual_sinks("selected.speaker", gain=0.15, commissioning_override=True,
                               backend=FakeAudioBackend(frozenset({"selected.speaker"})))
        self.assertEqual(enabled["audio"].gain, 0.15)
        self.assertEqual(set(enabled["audio"].clips), {MANUAL_CUE})

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "POSIX Unix socket required")
    def test_restricted_socket_manual_event_reaches_both_fake_sinks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "cue.sock"
            backend = FakeAudioBackend(frozenset({"selected.speaker"}))
            sinks = manual_sinks("selected.speaker", backend=backend)
            core = Coordinator(sinks, clock=lambda: NOW.timestamp())
            try:
                host = LocalServer(path, core)
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    self.skipTest("sandbox denies Unix socket bind")
                raise
            with host:
                worker = threading.Thread(target=host.serve_forever, daemon=True)
                worker.start()
                try:
                    event = manual_event(instant=NOW, event_id="synthetic-socket")
                    payload = {**event.__dict__, "occurred_at": event.occurred_at.isoformat(),
                               "observed_at": event.observed_at.isoformat()}
                    result = submit(path, json.dumps(payload).encode())
                    self.assertEqual(result["channels"]["text"], "accepted")
                    self.assertEqual(result["channels"]["audio"], "accepted")
                    self.assertEqual(len(backend.starts), 1)
                    from starforge_cues.cli import main
                    with patch("starforge_cues.manual_host.manual_event",
                               return_value=manual_event(instant=NOW, event_id="cli-submit")):
                        with redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(main(["manual-submit", "--socket", str(path)]), 0)
                    self.assertEqual(json.loads(output.getvalue())["channels"]["audio"], "accepted")
                    self.assertEqual(len(backend.starts), 2)
                finally:
                    host.shutdown()
                    worker.join()
            self.assertFalse(path.exists())
