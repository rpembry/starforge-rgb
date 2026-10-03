"""Private host settings and fake-only reload/recovery checks."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from starforge_cues import Coordinator, CueEvent, FakeSink
from starforge_cues.host_config import (ConfigError, parse_host_config, read_private_config,
                                        recover_private_config, reload_private_config)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "examples/synthetic-cue.json").read_text())
NOW = datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()


def document(generation=1, cue="ambient.first", manual=False, windows=True):
    return {"version": 1,
            "baseline": {"generation": generation, "cue_id": cue, "text": None},
            "quiet": {"manual": manual, "dnd": False, "muted": [], "timezone": "UTC",
                      "windows": ([{"start_minute": 1380, "end_minute": 420}] if windows else [])}}


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


class ParseTests(unittest.TestCase):
    def test_strict_versioned_data_and_quiet_window(self):
        config = parse_host_config(encoded(document()))
        self.assertEqual(config.generation, 1)
        self.assertEqual(config.cue_id, "ambient.first")
        self.assertFalse(config.policy.permits("rgb", datetime(2026, 10, 3, 23, 30,
                                                             tzinfo=timezone.utc).timestamp()))
        self.assertTrue(config.policy.permits("rgb", datetime(2026, 10, 3, 12, 0,
                                                            tzinfo=timezone.utc).timestamp()))

    def test_rejects_unknown_duplicate_oversized_and_unsafe_fields(self):
        bad = []
        for field, value in (("version", 2), ("version", True)):
            item = document()
            item[field] = value
            bad.append(encoded(item))
        item = document()
        item["driver"] = "shell"
        bad.append(encoded(item))
        item = document()
        item["quiet"]["muted"] = ["rgb", "rgb"]
        bad.append(encoded(item))
        item = document()
        item["quiet"]["manual"] = 0
        bad.append(encoded(item))
        item = document()
        item["quiet"]["timezone"] = "../private"
        bad.append(encoded(item))
        item = document()
        item["quiet"]["windows"][0]["end_minute"] = 1440
        bad.append(encoded(item))
        item = document()
        item["baseline"]["text"] = "\x00secret"
        bad.append(encoded(item))
        for unsafe_text in ("label\x7f", "right\u202eto-left", "isolate\u2066"):
            item = document()
            item["baseline"]["text"] = unsafe_text
            bad.append(encoded(item))
        bad.extend((b'{"version":1,"version":1}', b"{", b" " * 4097))
        for raw in bad:
            with self.subTest(raw=raw[:80]), self.assertRaises(ConfigError):
                parse_host_config(raw)
        core = Coordinator(clock=lambda: NOW)
        for unsafe_text in ("label\x7f", "right\u202eto-left"):
            with self.subTest(unsafe_text=unsafe_text), self.assertRaises(ValueError):
                core.set_baseline(1, "ambient.example", unsafe_text)
        core.set_baseline(1, "ambient.example", "A plain label\nwith a line break")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private files required")
class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "settings.json"

    def write(self, value, mode=0o600):
        self.path.write_bytes(encoded(value))
        self.path.chmod(mode)

    def test_rejects_permissive_file_directory_symlink_and_hardlink(self):
        self.write(document(), 0o644)
        with self.assertRaises(ConfigError):
            read_private_config(self.path)
        self.path.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaises(ConfigError):
            read_private_config(self.path)
        self.root.chmod(0o700)
        self.assertEqual(read_private_config(self.path).cue_id, "ambient.first")
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            read_private_config(link)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(ConfigError):
            read_private_config(self.path)
        pipe = self.root / "pipe.json"
        os.mkfifo(pipe, 0o600)
        with self.assertRaises(ConfigError):
            read_private_config(pipe)

    def test_reload_is_atomic_for_invalid_input_and_uses_current_baseline(self):
        clock = [NOW]
        sinks = {name: FakeSink() for name in ("text", "rgb", "audio")}
        core = Coordinator(sinks, clock=lambda: clock[0])
        self.write(document(windows=False))
        self.assertEqual(reload_private_config(self.path, core)["result"], "loaded")
        self.assertEqual(sinks["rgb"].state["rgb"]["cue_id"], "ambient.first")
        core.handle(CueEvent.from_mapping(FIXTURE))
        audio_before = len(sinks["audio"].calls)

        self.write(document(2, "ambient.private", manual=True, windows=False))
        self.assertEqual(reload_private_config(self.path, core)["result"], "loaded")
        for sink in sinks.values():
            self.assertIsNone(next(iter(sink.state.values())))
        self.assertEqual(len(sinks["audio"].calls), audio_before + 1)  # clear only

        self.write(document(3, "ambient.bad", manual=False, windows=False), mode=0o644)
        self.assertEqual(reload_private_config(self.path, core)["reason"], "invalid_config")
        self.assertTrue(core.policy.quiet)
        self.assertEqual(core._baseline_generation, 2)
        self.path.chmod(0o600)
        self.assertEqual(reload_private_config(self.path, core)["result"], "loaded")
        self.assertFalse(core.policy.quiet)
        self.assertEqual(len(sinks["audio"].calls), audio_before + 1)  # no old audio replay
        self.write(document(3, "ambient.stale", manual=True, windows=False))
        self.assertEqual(reload_private_config(self.path, core)["reason"], "invalid_config")
        self.assertFalse(core.policy.quiet)
        self.assertEqual(core._baseline_generation, 3)
        clock[0] += 241
        core.tick()
        self.assertEqual(sinks["rgb"].state["rgb"]["cue_id"], "ambient.bad")

    def test_missing_invalid_and_valid_restart(self):
        rgb = FakeSink()
        fresh, status = recover_private_config(self.path, sinks={"rgb": rgb}, clock=lambda: NOW)
        self.assertEqual(status, "missing")
        self.assertTrue(fresh.policy.quiet)
        self.assertIsNone(rgb.state["rgb"])
        self.write(document(4, "ambient.current", windows=False), mode=0o644)
        invalid, status = recover_private_config(self.path, sinks={"rgb": rgb}, clock=lambda: NOW)
        self.assertEqual(status, "invalid")
        self.assertTrue(invalid.policy.quiet)
        self.path.chmod(0o600)
        loaded, status = recover_private_config(self.path, sinks={"rgb": rgb}, clock=lambda: NOW)
        self.assertEqual(status, "loaded")
        self.assertEqual(loaded.recovery_result["previous_output"], "unknown")
        self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.current")
        self.assertEqual(len(loaded._leases), 0)
        self.path.unlink()
        self.assertEqual(reload_private_config(self.path, loaded)["reason"], "invalid_config")
        self.assertEqual(loaded._baseline_generation, 4)

    def test_first_valid_generation_one_after_fail_closed_startup(self):
        for initial in ("missing", "invalid"):
            with self.subTest(initial=initial):
                if initial == "invalid":
                    self.write(document(), mode=0o644)
                else:
                    self.path.unlink(missing_ok=True)
                rgb = FakeSink()
                core, status = recover_private_config(self.path, sinks={"rgb": rgb}, clock=lambda: NOW)
                self.assertEqual(status, initial)
                self.assertTrue(core.policy.quiet)
                self.assertIsNone(rgb.state["rgb"])

                self.write(document(1, "ambient.first", windows=False))
                self.assertEqual(reload_private_config(self.path, core)["result"], "loaded")
                self.assertFalse(core.policy.quiet)
                self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.first")
                self.assertEqual(reload_private_config(self.path, core)["reason"], "invalid_config")
                self.write(document(2, "ambient.next", windows=False))
                self.assertEqual(reload_private_config(self.path, core)["result"], "loaded")
                self.assertEqual(rgb.state["rgb"]["cue_id"], "ambient.next")


if __name__ == "__main__":
    unittest.main()
