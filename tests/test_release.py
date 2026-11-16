"""Install the built archive with no checkout client on the import path."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from tools.build_release import build
from tools.install_integration import ROOT


class ReleaseTests(unittest.TestCase):
    def test_reproducible_archive_contains_a_standalone_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build(root / "first")
            second = build(root / "second")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            component = root / "installed"
            with ZipFile(first) as bundle:
                names = set(bundle.namelist())
                self.assertTrue({"manifest.json", "services.yaml", "intermediate.pem", "translations/en.json",
                                 "brand/icon.png", "myenergy/client.py", "LICENSE", "NOTICE"} <= names)
                self.assertFalse(any(name.startswith(("custom_components/", "tests/", "tools/")) for name in names))
                self.assertFalse(any(name.endswith((".pyc", ".log", ".cookies", ".har")) for name in names))
                bundle.extractall(component)
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "from myenergy import MyEnergyClient; from myenergy.nem12 import parse_nem12; "
                 "assert callable(parse_nem12)", str(component)],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_tag_cannot_build_a_release(self):
        version = json.loads((ROOT / "custom_components/powercor_myenergy/manifest.json").read_text())["version"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "tag must equal"):
                build(Path(directory), tag=version + ".different")
