"""Assemble the HA component and standalone client into a config directory."""

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def install(config_dir):
    target = config_dir / "custom_components" / "powercor_myenergy"
    component = ROOT / "custom_components" / "powercor_myenergy"
    # Ship source and declared assets, never arbitrary local config or logs.
    files = [(path, path.relative_to(component)) for path in component.glob("*.py")]
    for pattern in ("translations/*.json", "brand/*.png"):
        files += [(path, path.relative_to(component)) for path in component.glob(pattern)]
    files += [(component / name, Path(name)) for name in
              ("manifest.json", "services.yaml", "intermediate.pem")]
    # Keep one maintained source for the client, with no HA dependencies.
    files += [(path, Path("myenergy") / path.name) for path in (ROOT / "myenergy").glob("*.py")]
    files += [(ROOT / name, Path(name)) for name in ("LICENSE", "NOTICE")]
    for source, relative in files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    args = parser.parse_args()
    install(args.config_dir)
    print("Installed Powercor / CitiPower myEnergy")
