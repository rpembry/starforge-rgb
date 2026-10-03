"""Theme validation tests only inspect synthetic bytes; no media is decoded or played."""

import hashlib
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from starforge_cues import CueEvent, ThemeError, load_directory

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / "examples/minimal-theme.json").read_text())
EVENT = json.loads((ROOT / "examples/synthetic-cue.json").read_text())


def make_pack(directory: Path, manifest: dict, content: bytes | None = None):
    directory.mkdir()
    if content is not None:
        (directory / "assets").mkdir()
        (directory / "assets" / "tone.wav").write_bytes(content)
    (directory / "manifest.json").write_text(json.dumps(manifest))


class ThemeTests(unittest.TestCase):
    def test_cli_preview_is_data_only(self):
        from starforge_cues.cli import main
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, EXAMPLE)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["theme-preview", "--directory", str(path), "--file",
                             str(ROOT / "examples/synthetic-cue.json"), "--rgb-cap", "0.1"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["channels"]["rgb"]["level"], 0.1)

    def test_minimal_resolve_and_silent_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, EXAMPLE)
            theme = load_directory(path)
            event = CueEvent.from_mapping({**EVENT, "text": "<synthetic>"})
            plan = theme.resolve(event, frozenset({"text", "rgb", "audio"}), rgb_level_cap=0.1)
            self.assertEqual(plan["text"]["content"], "Done: &lt;synthetic&gt;")
            self.assertEqual(plan["rgb"]["level"], 0.1)
            self.assertEqual(plan["audio"]["outcome"], "silent")
            self.assertEqual(theme.resolve(CueEvent.from_mapping({**EVENT, "text": None}),
                                           frozenset({"text"}))["text"]["outcome"], "silent")
            other = CueEvent.from_mapping({**EVENT, "cue_id": "unknown.cue"})
            self.assertEqual(theme.resolve(other, frozenset({"rgb"}))["rgb"]["outcome"], "silent")

    def test_asset_digest_license_and_reference(self):
        content = b"synthetic non-playable fixture"
        manifest = json.loads(json.dumps(EXAMPLE))
        manifest["capabilities"].append("audio")
        manifest["assets"] = {"assets/tone.wav": {
            "sha256": hashlib.sha256(content).hexdigest(), "license": "CC0-1.0",
            "provenance": "Synthetic bytes in unit test"}}
        manifest["cues"]["job.completed"]["audio"] = {"asset": "assets/tone.wav"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, manifest, content)
            theme = load_directory(path)
            plan = theme.resolve(CueEvent.from_mapping(EVENT), frozenset({"audio"}))
            self.assertEqual(plan["audio"]["asset"], "assets/tone.wav")
            (path / "assets" / "tone.wav").write_bytes(b"tampered")
            with self.assertRaises(ThemeError):
                load_directory(path)

    def test_reject_traversal_scripts_collisions_and_unknown_fields(self):
        invalid = []
        script = {**EXAMPLE, "script": "echo unsafe"}
        invalid.append(script)
        bad_version = {**EXAMPLE, "schema_version": 2}
        invalid.append(bad_version)
        for asset_path in ("../tone.wav", "/tmp/tone.wav", "C:/tone.wav", "assets/CON.wav",
                           "assets/a\\b.wav", "assets/a?.wav"):
            item = json.loads(json.dumps(EXAMPLE))
            item["assets"] = {asset_path: {"sha256": "0" * 64, "license": "MIT", "provenance": "Synthetic"}}
            invalid.append(item)
        for manifest in invalid:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "pack"
                make_pack(path, manifest)
                with self.assertRaises(ThemeError):
                    load_directory(path)

    def test_casefold_asset_collision(self):
        content = b"synthetic"
        manifest = json.loads(json.dumps(EXAMPLE))
        manifest["assets"] = {name: {"sha256": hashlib.sha256(content).hexdigest(),
                                      "license": "MIT", "provenance": "Synthetic"}
                              for name in ("assets/Tone.wav", "assets/tone.wav")}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, manifest)
            (path / "assets").mkdir()
            (path / "assets" / "Tone.wav").write_bytes(content)
            (path / "assets" / "tone.wav").write_bytes(content)
            with self.assertRaisesRegex(ThemeError, "colliding"):
                load_directory(path)

    def test_reject_symlinks_hardlinks_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, EXAMPLE)
            (path / "unexpected.py").write_text("pass")
            with self.assertRaises(ThemeError):
                load_directory(path)
            (path / "unexpected.py").unlink()
            (path / "assets").symlink_to(Path(temp), target_is_directory=True)
            with self.assertRaises(ThemeError):
                load_directory(path)
            (path / "assets").unlink()
            os.link(path / "manifest.json", path / "other.json")
            with self.assertRaises(ThemeError):
                load_directory(path)

    def test_duplicate_manifest_keys_and_asset_count_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pack"
            make_pack(path, EXAMPLE)
            (path / "manifest.json").write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ThemeError, "duplicate"):
                load_directory(path)
            too_many = json.loads(json.dumps(EXAMPLE))
            too_many["assets"] = {f"assets/{i}.wav": {"sha256": "0" * 64,
                                                         "license": "MIT", "provenance": "Synthetic"}
                                  for i in range(33)}
            (path / "manifest.json").write_text(json.dumps(too_many))
            with self.assertRaisesRegex(ThemeError, "too many"):
                load_directory(path)


if __name__ == "__main__":
    unittest.main()
