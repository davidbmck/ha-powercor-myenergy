"""Run isolated HA checks; always remove the container and its temporary storage."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import uuid

from build_release import build

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ghcr.io/home-assistant/home-assistant:stable")
    parser.add_argument("--live", type=Path, metavar="SECRETS_JSON",
                        help="Also run a fresh login and recovery using private local settings")
    args = parser.parse_args()
    build(ROOT / "dist")
    payload = None
    if args.live:
        if str(args.live) == "-":
            payload = json.dumps(json.load(sys.stdin)).encode()
        elif args.live.stat().st_mode & 0o077:
            parser.error("The secrets file must be readable only by its owner")
        else:
            # Pass secrets through stdin, never Docker args/env.
            payload = json.dumps(json.loads(args.live.read_text())).encode()
    name = "powercor-myenergy-test-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--rm", "--pull=never", "--name", name,
               "--network", "bridge" if args.live else "none",
               "--tmpfs", "/config:mode=0700",
               "--mount", f"type=bind,src={ROOT},dst=/source,readonly",
               "--entrypoint", "python"]
    if payload is not None:
        command.append("-i")
    command += [args.image, "/source/tests_ha/run.py", "--artifact", "/source/dist/powercor_myenergy.zip"]
    if args.live:
        command.append("--live")
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        return subprocess.run(command, input=payload).returncode
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        remaining = subprocess.run(["docker", "inspect", name], capture_output=True)
        if remaining.returncode == 0:
            raise RuntimeError("Test container cleanup failed")
        print("Disposable HA container removed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
