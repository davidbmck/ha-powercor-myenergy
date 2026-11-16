"""Build the self-contained component ZIP used by Marketplace and HACS."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.install_integration import ROOT, install


def build(output_dir, *, tag=None):
    manifest = json.loads((ROOT / "custom_components/powercor_myenergy/manifest.json").read_text())
    metadata = json.loads((ROOT / "hacs.json").read_text())
    if tag is not None and tag != manifest["version"]:
        raise ValueError("Release tag must equal the integration manifest version")
    if not metadata.get("zip_release") or metadata["filename"] != "powercor_myenergy.zip":
        raise ValueError("Release metadata must select powercor_myenergy.zip")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / metadata["filename"]
    with tempfile.TemporaryDirectory() as directory:
        component = install(Path(directory))
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
            for source in sorted(component.rglob("*")):
                if not source.is_file():
                    continue
                # Fixed timestamps, permissions and order make identical inputs reproducible.
                info = ZipInfo(source.relative_to(component).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = ZIP_DEFLATED
                bundle.writestr(info, source.read_bytes(), compresslevel=9)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", help="Require an exact match with manifest.json")
    args = parser.parse_args()
    print(build(args.output_dir, tag=args.tag).name)
