"""Catch obvious accidental disclosure in files admitted by the default-deny tree."""

from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = re.compile(r"^(?:\.gitignore|README\.md|LICENSE|pyproject\.toml|src/starforge_cues/[^/]+\.py|tests/test_[^/]+\.py|examples/[^/]+\.json|docs/[^/]+\.md|docs/adr/[^/]+\.md)$")
FORBIDDEN = (b"BEGIN " + b"PRIVATE KEY", b"ghp" + b"_", b"sk-proj" + b"-",
             b"/home/" + b"rpembry/", b"X-API-" + b"Key:")


class PublicTreeTests(unittest.TestCase):
    def test_tracked_paths_and_contents(self):
        paths = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).split(b"\0")
        for raw in filter(None, paths):
            path = raw.decode()
            with self.subTest(path=path):
                self.assertRegex(path, ALLOWED)
                data = (ROOT / path).read_bytes()
                for needle in FORBIDDEN:
                    self.assertNotIn(needle, data)

    def test_runtime_config_is_ignored(self):
        for path in ("config.json", "secrets.json", "src/starforge_cues/runtime.json", "examples/private.txt"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, path)


if __name__ == "__main__":
    unittest.main()
