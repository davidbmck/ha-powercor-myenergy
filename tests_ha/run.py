"""Stage the component into temporary HA storage and run offline boundary tests."""

import os
import asyncio
from pathlib import Path
import sys
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from install_integration import install

if "--artifact" in sys.argv:
    artifact = Path(sys.argv[sys.argv.index("--artifact") + 1])
    target = Path("/config/custom_components/powercor_myenergy")
    target.mkdir(parents=True, exist_ok=True)
    with ZipFile(artifact) as bundle:
        bundle.extractall(target)
else:
    install(Path("/config"))
os.chdir("/config")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/config")

suite = unittest.defaultTestLoader.discover(str(ROOT / "tests_ha"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise SystemExit(1)
if "--live" in sys.argv:
    from live_check import run
    asyncio.run(run())
